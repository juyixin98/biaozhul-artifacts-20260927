//! Run-scoped diagnostics: run ids plus replayable event logs.
//!
//! Every check (server or CLI) gets a `run-<unix>-<counter>` id and emits a
//! small structured event stream capturing the intermediate states needed to
//! replay the decision later: compilation summary, search milestones, the
//! chosen counterexample word or the bound that was hit.
//!
//! Set `WTIO_LOG_DIR=/some/dir` to additionally persist each run as
//! `<dir>/<run_id>.jsonl`. Test runs point this at a scratch directory so logs
//! survive for after-the-fact diagnosis.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;

static COUNTER: AtomicU64 = AtomicU64::new(0);

pub fn new_run_id() -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("run-{ts}-{n}")
}

#[derive(Debug, Clone, Serialize)]
pub struct Event {
    pub run_id: String,
    pub seq: u64,
    pub kind: String,
    #[serde(skip_serializing_if = "serde_json::Value::is_null")]
    pub detail: serde_json::Value,
}

pub struct RunLogger {
    run_id: String,
    seq: u64,
    file: Option<Mutex<File>>,
    log_dir: Option<PathBuf>,
}

impl RunLogger {
    pub fn new(run_id: String) -> Self {
        let log_dir = std::env::var("WTIO_LOG_DIR").ok().filter(|s| !s.is_empty()).map(PathBuf::from);
        let file = if let Some(dir) = &log_dir {
            // Best effort: never fail a check because logging setup fails.
            match std::fs::create_dir_all(dir)
                .and_then(|_| OpenOptions::new().create(true).append(true).open(dir.join(format!("{run_id}.jsonl"))))
            {
                Ok(f) => Some(Mutex::new(f)),
                Err(e) => {
                    eprintln!("warning: cannot open run log in {}: {e}", dir.display());
                    None
                }
            }
        } else {
            None
        };
        Self {
            run_id,
            seq: 0,
            file,
            log_dir,
        }
    }

    pub fn id(&self) -> &str {
        &self.run_id
    }

    pub fn log_path(&self) -> Option<PathBuf> {
        self.log_dir
            .as_ref()
            .map(|d| d.join(format!("{}.jsonl", self.run_id)))
    }

    pub fn event(&mut self, kind: impl Into<String>, detail: serde_json::Value) {
        self.seq += 1;
        let ev = Event {
            run_id: self.run_id.clone(),
            seq: self.seq,
            kind: kind.into(),
            detail,
        };
        if let Some(file) = &self.file {
            if let Ok(mut f) = file.lock() {
                if let Ok(line) = serde_json::to_string(&ev) {
                    let _ = writeln!(f, "{line}");
                }
            }
        }
        // Concise human-readable mirror on stderr; tests can opt in with
        // WTIO_LOG_STDERR=1.
        if std::env::var("WTIO_LOG_STDERR").is_ok() {
            eprintln!("[{}] #{} {}", ev.run_id, ev.seq, ev.kind);
        }
    }
}
