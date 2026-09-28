//! Structured per-request logging.
//!
//! Every request gets a `run_id` (UUID v4) which is returned in the
//! `x-run-id` response header, attached to tracing spans, and appended as one
//! JSON line to `requests.jsonl` when `FM_LOG_DIR` is configured. The line
//! records key intermediate state — interval bounds, hit count, timing, the
//! error category/code on failure — precisely so a problem can be replayed.

use std::fs::OpenOptions;
use std::path::PathBuf;
use std::sync::Mutex;

use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct RequestRecord {
    pub run_id: String,
    pub ts_unix_ms: u128,
    pub method: String,
    pub path: String,
    pub status: u16,
    pub duration_ms: u128,
    pub index: Option<String>,
    /// Key intermediate state for search/verify requests.
    pub interval: Option<(u64, u64)>,
    pub hit_count: Option<u64>,
    pub patterns: Option<usize>,
    pub error_category: Option<&'static str>,
    pub error_code: Option<&'static str>,
}

pub struct RequestLog {
    sink: Option<Mutex<PathBuf>>,
}

impl RequestLog {
    pub fn new(log_dir: Option<&std::path::Path>) -> Self {
        let sink = log_dir.map(|d| Mutex::new(d.join("requests.jsonl")));
        if let Some(dir) = log_dir {
            let _ = std::fs::create_dir_all(dir);
        }
        Self { sink }
    }

    pub fn record(&self, rec: &RequestRecord) {
        let Some(lock) = &self.sink else {
            return;
        };
        let line = match serde_json::to_string(rec) {
            Ok(s) => s,
            Err(_) => return,
        };
        // Lock poisoning must not take the service down; recover.
        let path_guard = match lock.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        if OpenOptions::new()
            .create(true)
            .append(true)
            .open(&*path_guard)
            .and_then(|mut f| {
                use std::io::Write;
                writeln!(f, "{line}")
            })
            .is_err()
        {
            tracing::warn!(run_id = %rec.run_id, "failed to append request log");
        }
    }
}
