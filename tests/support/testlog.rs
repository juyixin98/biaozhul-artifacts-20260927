//! Deterministic test logging: one JSON line per decision, prefixed with a
//! run id so a test run's log can be correlated to its inputs and verdicts.
//!
//! Lines go to stderr (visible with `cargo test -- --nocapture`) and are
//! appended to `target/hcomp-test-logs/<run_id>.jsonl` when that directory is
//! writable. Nothing here is used by production code.

#![allow(dead_code)]

use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

static RUN_ID: OnceLock<String> = OnceLock::new();

pub fn run_id() -> &'static str {
    RUN_ID.get_or_init(|| {
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        format!("test-{millis:x}-{:016x}", entropy())
    })
}

fn entropy() -> u64 {
    let a = &0u8 as *const u8 as u64;
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos() as u64)
        .unwrap_or(0);
    a ^ nanos.wrapping_mul(0x9E37_79B9_7F4A_7C15)
}

/// Log one structured decision. `input` identifies the vector/case,
/// `verdict` is pass/fail/expected-error, and `detail` carries the concrete
/// evidence (lengths, CRC category, block count, etc.).
pub fn log(suite: &str, input: &str, verdict: &str, detail: serde_json::Value) {
    let line = serde_json::json!({
        "run_id": run_id(),
        "suite": suite,
        "crate_version": env!("CARGO_PKG_VERSION"),
        "format": "HCMP",
        "format_version": 1,
        "input": input,
        "verdict": verdict,
        "detail": detail,
    })
    .to_string();
    eprintln!("{line}");

    let log_dir = concat!(env!("CARGO_MANIFEST_DIR"), "/target/hcomp-test-logs");
    if std::fs::create_dir_all(log_dir).is_ok() {
        let path = std::path::Path::new(log_dir).join(format!("{}.jsonl", run_id()));
        use std::io::Write;
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
        {
            let _ = writeln!(f, "{line}");
        }
    }
}
