//! Structured per-run test logging.
//!
//! Each test creates one [`RunLog`] with a unique run id. Inputs, key
//! intermediate states (graph statistics, per-pass relaxation counts,
//! extracted cycle / assignment) and the final verdict with its reason are
//! appended as JSON objects and flushed to `test-results/runs.jsonl` when the
//! logger is dropped (or explicitly via [`RunLog::flush`]).
//!
//! The same run id is printed by the test harness output, so a failing test
//! can be replayed by searching the file for that id. The log is plain JSONL
//! (one JSON object per line) so it is greppable and language-independent.

use std::fs::{create_dir_all, File, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::sync::Mutex;

use serde::Serialize;
use std::collections::BTreeMap;
use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

/// Directory (relative to the crate root / current working dir) where JSONL
/// run logs are written.
pub const LOG_DIR: &str = "test-results";
pub const LOG_FILE: &str = "runs.jsonl";

/// One per-process output file, appended by every test in the process.
fn shared_file() -> &'static Mutex<File> {
    static FILE: OnceLock<Mutex<File>> = OnceLock::new();
    FILE.get_or_init(|| {
        let dir = Path::new(LOG_DIR);
        create_dir_all(dir).expect("create test-results dir");
        let f = OpenOptions::new()
            .create(true)
            .append(true)
            .open(dir.join(LOG_FILE))
            .expect("open runs.jsonl");
        Mutex::new(f)
    })
}

/// The verdict of a run.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "result", rename_all = "snake_case")]
pub enum Verdict {
    Pass { reason: String },
    Fail { reason: String },
    /// The run exercised an expected error.
    ExpectedError { kind: String, reason: String },
}

/// A single run: one feature/scenario with its evidence trail.
pub struct RunLog {
    run_id: String,
    test_name: String,
    started_at_nanos: u128,
    events: Vec<serde_json::Value>,
    verdict: Option<Verdict>,
    flushed: bool,
}

impl RunLog {
    /// Start a new run. The id is `t-<nanos>-<test_name>`; uniqueness across
    /// parallel tests relies on nanosecond time plus the distinct test name.
    pub fn start(test_name: impl Into<String>) -> Self {
        let test_name = test_name.into();
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let safe: String = test_name
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() || c == '_' { c } else { '-' })
            .collect();
        let run_id = format!("t-{nanos:x}-{safe}");
        RunLog {
            run_id,
            test_name: safe,
            started_at_nanos: nanos,
            events: Vec::new(),
            verdict: None,
            flushed: false,
        }
    }

    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    /// Record an input exactly as the test constructed it.
    pub fn input(&mut self, description: impl Into<String>, value: impl Serialize) {
        self.event("input", description.into(), value);
    }

    /// Record a key intermediate state (graph stats, trace snapshot, etc.).
    pub fn state(&mut self, description: impl Into<String>, value: impl Serialize) {
        self.event("intermediate_state", description.into(), value);
    }

    /// Record a judgment made during the test and why.
    pub fn reasoning(&mut self, judgment: impl Into<String>, why: impl Into<String>) {
        let mut m = BTreeMap::new();
        m.insert("judgment".to_string(), serde_json::Value::String(judgment.into()));
        m.insert("why".to_string(), serde_json::Value::String(why.into()));
        self.event_raw("judgment", serde_json::to_value(m).unwrap());
    }

    /// Record an error the run observed.
    pub fn observed_error(&mut self, kind: impl Into<String>, message: impl Into<String>) {
        let mut m = BTreeMap::new();
        m.insert("kind".to_string(), serde_json::Value::String(kind.into()));
        m.insert(
            "message".to_string(),
            serde_json::Value::String(message.into()),
        );
        self.event_raw("observed_error", serde_json::to_value(m).unwrap());
    }

    fn event(&mut self, kind: &str, description: String, value: impl Serialize) {
        let v = serde_json::to_value(value).unwrap_or(serde_json::json!("<unserializable>"));
        let mut m = BTreeMap::new();
        m.insert("description".to_string(), serde_json::Value::String(description));
        m.insert("value".to_string(), v);
        self.event_raw(kind, serde_json::to_value(m).unwrap());
    }

    fn event_raw(&mut self, kind: &str, payload: serde_json::Value) {
        let seq = self.events.len();
        let mut record = payload.as_object().cloned().unwrap_or_default();
        record.insert("event".to_string(), serde_json::Value::String(kind.to_string()));
        record.insert("seq".to_string(), serde_json::json!(seq));
        self.events.push(serde_json::Value::Object(record));
    }

    pub fn pass(mut self, reason: impl Into<String>) {
        self.verdict = Some(Verdict::Pass {
            reason: reason.into(),
        });
        self.flush();
    }

    pub fn fail(mut self, reason: impl Into<String>) {
        self.verdict = Some(Verdict::Fail {
            reason: reason.into(),
        });
        self.flush();
    }

    pub fn expected_error(mut self, kind: impl Into<String>, reason: impl Into<String>) {
        self.verdict = Some(Verdict::ExpectedError {
            kind: kind.into(),
            reason: reason.into(),
        });
        self.flush();
    }

    pub fn flush(&mut self) {
        if self.flushed {
            return;
        }
        let record = serde_json::json!({
            "run_id": self.run_id,
            "test": self.test_name,
            "started_at_nanos": self.started_at_nanos,
            "events": self.events,
            "verdict": self.verdict,
        });
        let line = serde_json::to_string(&record).expect("run record serializes");
        {
            let mut f = shared_file().lock().expect("log file mutex");
            writeln!(f, "{line}").expect("write run record");
        }
        self.flushed = true;
        // Surface the run id on stdout so the test report links to the log.
        println!("[testlog] {} -> {}/{}", self.run_id, LOG_DIR, LOG_FILE);
    }
}

impl Drop for RunLog {
    fn drop(&mut self) {
        if !self.flushed {
            self.verdict
                .get_or_insert(Verdict::Fail {
                    reason: "run dropped without an explicit verdict".into(),
                });
            self.flush();
        }
    }
}
