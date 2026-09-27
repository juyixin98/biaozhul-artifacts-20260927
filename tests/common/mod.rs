//! Test support: fixture loading, JSONL replay logging and per-step driving.
//!
//! Every test logs one JSON object per significant event to
//! `test_artifacts/run-log.jsonl`.  Each entry carries the run id, the test
//! name, the intermediate monitor state (spawned/resolved/violated
//! obligation ids and the aggregate verdict) and the judgment reason, so a
//! failing run can be replayed from the log plus the fixture it names.

#![allow(dead_code)]

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

use bounded_monitor::kernel::{Limits, Monitor, ObligationStatus, ObligationView, StepOutcome, Verdict};
use bounded_monitor::language::{Ruleset, Step};
use bounded_monitor::oracle;
use serde::Serialize;

static LOG_LOCK: Mutex<()> = Mutex::new(());
static LOG_INIT: std::sync::Once = std::sync::Once::new();

/// Path of this test binary's own replay log.  `cargo test` runs every
/// integration-test target as a separate process in parallel; deriving the
/// file name from the executable stem means each target truncates only its
/// own log on startup, so parallel runs never erase one another's entries.
fn log_path() -> PathBuf {
    let stem = std::env::current_exe()
        .ok()
        .and_then(|p| p.file_stem().map(|s| s.to_string_lossy().into_owned()))
        .unwrap_or_else(|| "unknown-test".to_string());
    artifacts_dir().join(format!("run-log-{stem}.jsonl"))
}

/// Truncate this binary's replay log once per process.
pub fn init_log() {
    LOG_INIT.call_once(|| {
        let _guard = LOG_LOCK.lock().unwrap();
        File::create(log_path()).unwrap();
    });
}

/// One replay-log entry.
#[derive(Debug, Serialize)]
pub struct LogEntry {
    pub run_id: String,
    pub test: String,
    pub phase: String,
    pub step: Option<u64>,
    pub verdict: Option<&'static str>,
    pub spawned: Vec<String>,
    pub resolved: Vec<String>,
    pub violated: Vec<String>,
    pub pending: Vec<String>,
    pub detail: String,
}

pub fn artifacts_dir() -> PathBuf {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("test_artifacts");
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// Append one entry to this test binary's JSONL replay log.
pub fn log(entry: LogEntry) {
    init_log();
    let _guard = LOG_LOCK.lock().unwrap();
    let mut file = OpenOptions::new().create(true).append(true).open(log_path()).unwrap();
    writeln!(file, "{}", serde_json::to_string(&entry).unwrap()).unwrap();
}

pub fn log_state(run_id: &str, test: &str, phase: &str, outcome: &StepOutcome, obligations: &[ObligationView], detail: impl Into<String>) {
    log(LogEntry {
        run_id: run_id.to_string(),
        test: test.to_string(),
        phase: phase.to_string(),
        step: Some(outcome.index),
        verdict: Some(outcome.verdict.as_str()),
        spawned: outcome.spawned.clone(),
        resolved: outcome.resolved.clone(),
        violated: outcome.violated.clone(),
        pending: obligations
            .iter()
            .filter(|o| o.status == ObligationStatus::Pending)
            .map(|o| o.id.clone())
            .collect(),
        detail: detail.into(),
    });
}

pub fn log_event(run_id: &str, test: &str, phase: &str, detail: impl Into<String>) {
    log(LogEntry {
        run_id: run_id.to_string(),
        test: test.to_string(),
        phase: phase.to_string(),
        step: None,
        verdict: None,
        spawned: vec![],
        resolved: vec![],
        violated: vec![],
        pending: vec![],
        detail: detail.into(),
    });
}

/// Fresh log file (truncates the current test binary's log).  Normally
/// `init_log` handles this once per process; this is exposed for explicit
/// resets.
pub fn reset_log() {
    let _guard = LOG_LOCK.lock().unwrap();
    File::create(log_path()).unwrap();
}

fn fixtures_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures")
}

pub fn load_ruleset(name: &str) -> Ruleset {
    let path = fixtures_root().join("rulesets").join(name);
    let bytes = std::fs::read(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    serde_json::from_slice(&bytes).unwrap_or_else(|e| panic!("parse {}: {e}", path.display()))
}

#[derive(serde::Deserialize)]
struct TraceFile {
    #[allow(dead_code)]
    #[serde(default)]
    description: String,
    steps: Vec<Step>,
}

pub fn load_trace(name: &str) -> Vec<Step> {
    let path = fixtures_root().join("traces").join(name);
    let bytes = std::fs::read(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    let file: TraceFile =
        serde_json::from_slice(&bytes).unwrap_or_else(|e| panic!("parse {}: {e}", path.display()));
    file.steps
}

#[derive(serde::Deserialize, Debug)]
pub struct ExpectedObligation {
    pub id: String,
    pub rule_id: String,
    pub kind: String,
    pub status: String,
    pub trigger_step: Option<u64>,
    pub deadline_step: Option<u64>,
    pub ordinal: u64,
    pub resolution_step: Option<u64>,
    pub violation_step: Option<u64>,
    #[serde(default)]
    pub correlation: Option<bounded_monitor::kernel::Correlation>,
}

#[derive(serde::Deserialize, Debug)]
pub struct ExpectedFile {
    pub run_id: String,
    pub verdict: String,
    pub closed: bool,
    pub obligations: Vec<ExpectedObligation>,
}

pub fn load_expected(name: &str) -> ExpectedFile {
    let path = fixtures_root().join("expected").join(name);
    let bytes = std::fs::read(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    serde_json::from_slice(&bytes).unwrap_or_else(|e| panic!("parse {}: {e}", path.display()))
}

/// Drive every step through the kernel, logging intermediate states.
pub fn run_online(
    run_id: &str,
    test: &str,
    ruleset: &Ruleset,
    steps: &[Step],
    limits: Limits,
) -> Monitor {
    let mut monitor = Monitor::new(ruleset.clone(), limits).expect("monitor::new");
    for step in steps {
        let outcome = monitor.apply_step(step).expect("apply_step");
        log_state(
            run_id,
            test,
            "kernel_step",
            &outcome,
            &monitor.obligations(),
            format!("applied event type `{}`", step.event.event_type),
        );
    }
    monitor
}

/// Assert every categorical field of one obligation against a hand-computed
/// expectation file.
pub fn assert_obligation(view: &ObligationView, expected: &ExpectedObligation) {
    assert_eq!(view.id, expected.id, "obligation id");
    assert_eq!(view.rule_id, expected.rule_id, "{} rule_id", view.id);
    assert_eq!(format!("{:?}", view.kind).to_lowercase(), expected.kind, "{} kind", view.id);
    assert_eq!(
        format!("{:?}", view.status).to_lowercase(),
        expected.status,
        "{} status (reason={})",
        view.id,
        view.reason
    );
    assert_eq!(view.trigger_step, expected.trigger_step, "{} trigger_step", view.id);
    assert_eq!(view.deadline_step, expected.deadline_step, "{} deadline_step", view.id);
    assert_eq!(view.ordinal, expected.ordinal, "{} ordinal", view.id);
    assert_eq!(view.resolution_step, expected.resolution_step, "{} resolution_step", view.id);
    assert_eq!(view.violation_step, expected.violation_step, "{} violation_step", view.id);
    if let Some(want) = &expected.correlation {
        assert_eq!(view.correlation.as_ref(), Some(want), "{} correlation", view.id);
    }
}

/// Full cross-check used by every fixture test: hand-computed expected file
/// vs online kernel vs independent offline oracle, in both directions.
pub fn cross_check(run_id: &str, test: &str, ruleset_name: &str, trace_name: &str, expected_name: &str) {
    let ruleset = load_ruleset(ruleset_name);
    let steps = load_trace(trace_name);
    let expected = load_expected(expected_name);

    let monitor = run_online(run_id, test, &ruleset, &steps, Limits::default());
    let online = monitor.obligations();

    // Kernel vs hand-computed expectations.
    assert_eq!(
        monitor.verdict().as_str(),
        expected.verdict,
        "[{run_id}] kernel verdict: hand expected {} got {}",
        expected.verdict,
        monitor.verdict().as_str()
    );
    assert_eq!(monitor.is_closed(), expected.closed, "[{run_id}] closed flag");
    assert_eq!(
        online.len(),
        expected.obligations.len(),
        "[{run_id}] obligation count: expected {} (ids: {:?}), got {} (ids: {:?})",
        expected.obligations.len(),
        expected.obligations.iter().map(|o| o.id.clone()).collect::<Vec<_>>(),
        online.len(),
        online.iter().map(|o| o.id.clone()).collect::<Vec<_>>()
    );
    let online_by_id: std::collections::BTreeMap<_, _> =
        online.iter().map(|o| (o.id.clone(), o)).collect();
    for want in &expected.obligations {
        let view = online_by_id.get(&want.id).unwrap_or_else(|| panic!("missing obligation {}", want.id));
        assert_obligation(view, want);
    }

    // Independent oracle replay vs hand expectations.
    let report = oracle::evaluate(&ruleset, &steps).expect("oracle evaluate");
    assert_eq!(report.verdict.as_str(), expected.verdict, "[{run_id}] oracle verdict");
    assert_eq!(report.closed, expected.closed, "[{run_id}] oracle closed");
    let oracle_by_id: std::collections::BTreeMap<_, _> =
        report.obligations.iter().map(|o| (o.id.clone(), o)).collect();
    for want in &expected.obligations {
        let got = oracle_by_id.get(&want.id).unwrap_or_else(|| panic!("oracle missing {}", want.id));
        assert_eq!(format!("{:?}", got.kind).to_lowercase(), want.kind, "oracle {} kind", got.id);
        assert_eq!(format!("{:?}", got.status).to_lowercase(), want.status, "oracle {} status", got.id);
        assert_eq!(got.trigger_step, want.trigger_step, "oracle {} trigger", got.id);
        assert_eq!(got.deadline_step, want.deadline_step, "oracle {} deadline", got.id);
        assert_eq!(got.ordinal, want.ordinal, "oracle {} ordinal", got.id);
        assert_eq!(got.resolution_step, want.resolution_step, "oracle {} resolution", got.id);
        assert_eq!(got.violation_step, want.violation_step, "oracle {} violation", got.id);
    }

    // Oracle vs kernel: identical obligation id sets.
    let oracle_ids: std::collections::BTreeSet<_> = oracle_by_id.keys().collect();
    let online_ids: std::collections::BTreeSet<_> = online_by_id.keys().collect();
    assert_eq!(oracle_ids, online_ids, "[{run_id}] oracle/kernel obligation id sets differ");

    crate::common::log_event(
        run_id,
        test,
        "cross_check_ok",
        format!("kernel + oracle agree with hand expectation `{expected_name}` verdict={}", expected.verdict),
    );
}

/// Construct a simple step in code-based tests.
pub fn step(index: u64, event_type: &str, facts: serde_json::Value, end: bool) -> Step {
    let map = match facts {
        serde_json::Value::Object(m) => m,
        _ => panic!("facts must be a JSON object"),
    };
    Step {
        index,
        event: bounded_monitor::language::Event {
            event_type: event_type.to_string(),
            facts: map,
        },
        ruleset_version: None,
        end,
    }
}

pub fn verdict_str(v: Verdict) -> &'static str {
    v.as_str()
}
