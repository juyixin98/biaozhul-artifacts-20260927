//! Per-request run identity for log correlation.
//!
//! Every request receives a run id composed of a UTC timestamp, the server
//! pid and a monotonic counter, optionally overridden by the client's
//! `X-Run-Id` header so a test harness can force a known identity. The same id
//! appears in the JSON response, enabling logs <-> request <-> result
//! correlation.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static COUNTER: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, Clone)]
pub struct RunId(pub String);

impl RunId {
    pub fn generate(client_hint: Option<String>) -> RunId {
        if let Some(hint) = client_hint.map(|s| s.trim().to_string()) {
            if !hint.is_empty() && hint.len() <= 128 && hint.chars().all(id_safe) {
                return RunId(hint);
            }
        }
        let micros = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_micros())
            .unwrap_or(0);
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        RunId(format!(
            "run-{:016x}-{:05}-{}",
            micros,
            std::process::id() % 100_000,
            n
        ))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

fn id_safe(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn honours_safe_client_hint() {
        let id = RunId::generate(Some("fixture-mutex-001".into()));
        assert_eq!(id.as_str(), "fixture-mutex-001");
    }

    #[test]
    fn ignores_unsafe_hint() {
        let id = RunId::generate(Some("bad id with spaces".into()));
        assert!(id.as_str().starts_with("run-"));
    }
}
