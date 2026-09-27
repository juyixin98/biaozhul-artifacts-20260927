//! Request-correlated progress logging.
//!
//! Every log line carries the run/request id so test logs can be tied back to
//! a specific input or invocation. Lines go to stderr through `tracing` and,
//! when `HUFF_LOG_DIR` is set, are mirrored to `run-<id>.log` files there.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::OnceLock;

use huff_core::FORMAT_VERSION;

static LOG_DIR: OnceLock<Option<PathBuf>> = OnceLock::new();

/// Initialise logging. Safe to call once at startup; later calls are ignored.
pub fn init(log_dir: Option<PathBuf>) {
    let dir = log_dir.clone();
    let _ = LOG_DIR.set(log_dir);
    if let Some(dir) = &dir {
        let _ = fs::create_dir_all(dir);
    }
    // Simple filter: RUST_LOG or info-level for our own targets.
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info,huff=debug,huff_core=warn"));
    let _ = tracing_subscriber::fmt().with_env_filter(filter).with_writer(std::io::stderr).try_init();
    tracing::info!(
        "logging initialised (format v{}){}",
        FORMAT_VERSION,
        dir.as_ref()
            .map(|d| format!(", per-run logs in {}", d.display()))
            .unwrap_or_default()
    );
}

/// Record one progress/judgment line for a run id.
pub fn record(run_id: &str, message: &str) {
    tracing::info!(run_id = %run_id, "{message}");
    if let Some(Some(dir)) = LOG_DIR.get() {
        let path = dir.join(format!("run-{run_id}.log"));
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(path) {
            let _ = writeln!(f, "[run {run_id}] {message}");
        }
    }
}
