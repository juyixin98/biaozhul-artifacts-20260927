//! Evidence support: a run recorder that persists replayable logs.
//!
//! Every integration test creates one [`RunRecorder`]. Records land in
//! `test-results/runs/<run-id>.log` (human-readable, one line per event) and,
//! at the end of the run, in `test-results/summary.jsonl` (one JSON object per
//! test process). Events carry:
//!
//! * the run id and sequential event number (enough to replay ordering);
//! * the named "key intermediate state" being inspected;
//! * the expected value, actual value and the judgement reason.
//!
//! Failures are recorded *before* the assertion panics, so a crashed run still
//! leaves an explanation on disk.

use std::fs::{self, File, OpenOptions};
use std::io::Result as IoResult;
use std::io::Write;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static RUN_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Timestamp formatted as YYYY-MM-DD HH:MM:SS UTC without external crates.
fn utc_timestamp() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let (is_leap, year, month, day, hour, min, sec) = civil_from_unix(secs);
    let _ = is_leap;
    format!("{year:04}-{month:02}-{day:02} {hour:02}:{min:02}:{sec:02} UTC")
}

/// Convert seconds since epoch to (leap, year, month, day, hh, mm, ss) UTC.
fn civil_from_unix(secs: u64) -> (bool, u64, u64, u64, u64, u64, u64) {
    let days = secs / 86_400;
    let rem = secs % 86_400;
    let hour = rem / 3600;
    let min = (rem % 3600) / 60;
    let sec = rem % 60;

    // Days since 1970-01-01 -> year (Howard Hinnant's civil-from-days).
    let z = days + 719_468;
    let era = z / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if m <= 2 { y + 1 } else { y };
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    (leap, year, m, d, hour, min, sec)
}

pub struct RunRecorder {
    run_id: String,
    test_name: String,
    log_path: std::path::PathBuf,
    events: u64,
    started_nanos: u128,
    events_jsonl: Vec<String>,
    finished: bool,
}

impl RunRecorder {
    /// Start a run for `test_name`. The id embeds wall-clock nanos and a process
    /// counter, so concurrent or repeated runs never collide.
    pub fn start(test_name: &str) -> Self {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let seq = RUN_COUNTER.fetch_add(1, Ordering::SeqCst);
        let run_id = format!("{test_name}-{nanos:x}-{seq}");
        let dir = results_dir().join("runs");
        fs::create_dir_all(&dir).expect("create test-results/runs");
        let log_path = dir.join(format!("{run_id}.log"));
        let mut f = File::create(&log_path).expect("create run log");
        writeln!(f, "# LZ77B evidence run: {run_id}").unwrap();
        writeln!(f, "# test: {test_name}").unwrap();
        writeln!(f, "# started: {}", utc_timestamp()).unwrap();
        writeln!(f, "# rustc: {}", rustc_version()).unwrap();
        writeln!(f, "# constants: WINDOW={} MIN_MATCH={} MAX_MATCH={} MAX_PAYLOAD={} MAX_OUTPUT={} MAX_EXPANSION={}",
            lz77b::core::constants::WINDOW_SIZE,
            lz77b::core::constants::MIN_MATCH,
            lz77b::core::constants::MAX_MATCH,
            lz77b::core::constants::MAX_PAYLOAD,
            lz77b::core::constants::MAX_OUTPUT,
            lz77b::core::constants::MAX_EXPANSION,
        ).unwrap();
        RunRecorder {
            run_id,
            test_name: test_name.to_string(),
            log_path,
            events: 0,
            started_nanos: nanos,
            events_jsonl: Vec::new(),
            finished: false,
        }
    }

    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    fn append_line(&mut self, level: &str, msg: &str) {
        self.events += 1;
        let line = format!(
            "{:03} [{}] {} :: {}",
            self.events,
            utc_timestamp(),
            level,
            msg
        );
        let mut f = OpenOptions::new()
            .append(true)
            .open(&self.log_path)
            .expect("open log");
        writeln!(f, "{line}").unwrap();
        self.events_jsonl.push(
            serde_json::json!({
                "n": self.events,
                "level": level,
                "message": msg,
            })
            .to_string(),
        );
    }

    /// Record an intermediate state under inspection.
    pub fn state(&mut self, name: &str, value: impl std::fmt::Display) {
        self.append_line("STATE", &format!("{name} = {value}"));
    }

    /// Record a check and its reasoning, then return whether it passed so the
    /// caller can assert.
    pub fn check(
        &mut self,
        name: &str,
        passed: bool,
        expected: &str,
        actual: &str,
        reason: &str,
    ) -> bool {
        self.append_line(
            if passed { "PASS" } else { "FAIL" },
            &format!("{name}: expected {expected}, actual {actual} — {reason}"),
        );
        passed
    }

    /// Record a failure and panic with a replayable message.
    pub fn fail(&mut self, name: &str, expected: &str, actual: &str, reason: &str) -> ! {
        self.append_line(
            "FAIL",
            &format!("{name}: expected {expected}, actual {actual} — {reason}"),
        );
        self.finish(false);
        panic!(
            "[{run}] {name}: expected {expected}, got {actual} — {reason}; see {log}",
            run = self.run_id,
            log = self.log_path.display()
        );
    }

    /// Assert with recorded context.
    pub fn assert_eq_display<T: PartialEq + std::fmt::Display>(
        &mut self,
        name: &str,
        actual: T,
        expected: T,
        reason: &str,
    ) {
        let pass = actual == expected;
        if !self.check(
            name,
            pass,
            &expected.to_string(),
            &actual.to_string(),
            reason,
        ) {
            self.fail(name, &expected.to_string(), &actual.to_string(), reason);
        }
    }

    pub fn note(&mut self, msg: &str) {
        self.append_line("NOTE", msg);
    }

    /// Close the run and append one summary line to summary.jsonl.
    pub fn finish(&mut self, passed: bool) {
        if self.finished {
            return;
        }
        self.finished = true;
        let elapsed_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
            .saturating_sub(self.started_nanos)
            / 1_000_000;
        self.append_line(
            "RESULT",
            &format!(
                "verdict={} events={} elapsed_ms={}",
                if passed { "PASS" } else { "FAIL" },
                self.events,
                elapsed_ms
            ),
        );
        let summary = serde_json::json!({
            "run_id": self.run_id,
            "test": self.test_name,
            "verdict": if passed { "PASS" } else { "FAIL" },
            "events": self.events,
            "elapsed_ms": elapsed_ms,
            "finished": true,
            "log": self.log_path.display().to_string(),
        });
        append_summary(&summary.to_string());
    }
}

impl Drop for RunRecorder {
    fn drop(&mut self) {
        if !self.finished {
            // Panicked midway: still leave a verdict so the summary is honest.
            self.append_line(
                "FAIL",
                "run ended without finish() — assertion panic or early return",
            );
            let summary = serde_json::json!({
                "run_id": self.run_id,
                "test": self.test_name,
                "verdict": "FAIL",
                "events": self.events,
                "finished": false,
                "log": self.log_path.display().to_string(),
            });
            append_summary(&summary.to_string());
        }
    }
}

fn results_dir() -> std::path::PathBuf {
    let manifest = env!("CARGO_MANIFEST_DIR");
    std::path::Path::new(manifest).join("test-results")
}

fn append_summary(line: &str) {
    // Several test binaries run concurrently and append to the same file.
    // Serialize writers with an atomic mkdir lock (no external crate needed).
    let dir = results_dir();
    let _ = fs::create_dir_all(&dir);
    let lock = dir.join(".summary.lock");
    for _ in 0..1000 {
        if fs::create_dir(&lock).is_ok() {
            let guard = SummaryLock(lock.clone());
            if let Ok(mut f) = OpenOptions::new()
                .create(true)
                .append(true)
                .open(dir.join("summary.jsonl"))
            {
                let _ = writeln!(f, "{line}");
            }
            drop(guard);
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(2));
    }
    // Lock contention is pathological here; don't lose the record entirely.
    let fallback = dir.join("summary-unlocked.log");
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(fallback) {
        let _ = writeln!(f, "{line}");
    }
}

struct SummaryLock(std::path::PathBuf);
impl Drop for SummaryLock {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn rustc_version() -> &'static str {
    option_env!("RUSTC_VERSION").unwrap_or("rustc (version captured at build time if set)")
}

/// Helper: ensure the results directory exists (used by build.rs-less setup).
pub fn ensure_results_dir() -> IoResult<()> {
    fs::create_dir_all(results_dir().join("runs"))
}
