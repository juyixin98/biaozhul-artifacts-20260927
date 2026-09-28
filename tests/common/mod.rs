//! Shared test harness.
//!
//! Drives a fixture spec (same JSON shape as `btmon replay`) through the
//! incremental kernel and, independently, through the set-based oracle. It
//! also appends one replayable JSONL line per state transition to
//! `target/replay/integration.jsonl`, carrying run id, intermediate states
//! and the reason code for every decision.

#![allow(dead_code)]

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

use serde::Deserialize;
use serde_json::{json, Value};

use btmon::lang::{Event, RuleSet};
use btmon::monitor::{Limits, Monitor, StepReport};
use btmon::reference::{self, RefInstance};
use std::collections::BTreeMap;

#[derive(Debug, Deserialize)]
pub struct RunFile {
    pub run_id: String,
    #[serde(default)]
    pub segments: Vec<SegmentSpec>,
    #[serde(default)]
    pub ruleset: Option<RuleSet>,
    #[serde(default)]
    pub events: Vec<Value>,
    #[serde(default)]
    pub closed: Option<bool>,
    /// Prefix verdict checks: after feeding the first N events of the
    /// (flattened) trace, the global verdict must equal the given value.
    #[serde(default)]
    pub open_checks: Vec<OpenCheck>,
}

#[derive(Debug, Deserialize)]
pub struct SegmentSpec {
    pub ruleset: RuleSet,
    #[serde(default)]
    pub events: Vec<Value>,
}

#[derive(Debug, Deserialize)]
pub struct OpenCheck {
    pub after_events: usize,
    pub global_verdict: String,
}

pub struct Driven {
    pub run_id: String,
    pub monitor: Monitor,
    pub reports: Vec<StepReport>,
    pub oracle_segments: Vec<(RuleSet, Vec<(i64, Event)>)>,
    pub events_total: usize,
}

pub fn fixture_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures")
}

pub fn load_run(name: &str) -> RunFile {
    let path = fixture_root().join("runs").join(format!("{name}.json"));
    let raw = fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    let mut f: RunFile =
        serde_json::from_str(&raw).unwrap_or_else(|e| panic!("parse {}: {e}", path.display()));
    if f.segments.is_empty() {
        let rs = f.ruleset.take().expect("spec needs segments or ruleset");
        f.segments.push(SegmentSpec {
            ruleset: rs,
            events: std::mem::take(&mut f.events),
        });
    }
    f
}

pub fn load_expected(name: &str) -> Value {
    let path = fixture_root().join("expected").join(format!("{name}.json"));
    let raw = fs::read_to_string(&path).unwrap();
    serde_json::from_str(&raw).unwrap()
}

fn parse_slot(v: Value) -> (Event, Option<i64>) {
    #[derive(Deserialize)]
    struct Tagged {
        event: Event,
        #[serde(default)]
        step: Option<i64>,
    }
    if let Ok(t) = serde_json::from_value::<Tagged>(v.clone()) {
        (t.event, t.step)
    } else {
        (serde_json::from_value(v).unwrap(), None)
    }
}

/// Feed a spec through the kernel (no closing).
pub fn drive(f: &RunFile) -> Driven {
    let limits = Limits::replay();
    let mut monitor = Monitor::new(
        "test".to_string(),
        f.run_id.clone(),
        f.segments[0].ruleset.clone(),
        limits,
    )
    .expect("valid initial ruleset");
    let mut reports = Vec::new();
    let mut oracle_segments: Vec<(RuleSet, Vec<(i64, Event)>)> = Vec::new();
    let mut events_total = 0;
    for (idx, seg) in f.segments.iter().enumerate() {
        if idx > 0 {
            monitor.rotate(seg.ruleset.clone()).expect("rotate");
        }
        let mut slice = Vec::new();
        for slot in &seg.events {
            let (ev, step) = parse_slot(slot.clone());
            let report = monitor
                .append(&ev, step)
                .unwrap_or_else(|e| panic!("append at step {}: {e}", monitor.next_step));
            slice.push((report.step, ev));
            reports.push(report);
            events_total += 1;
        }
        oracle_segments.push((seg.ruleset.clone(), slice));
    }
    Driven {
        run_id: f.run_id.clone(),
        monitor,
        reports,
        oracle_segments,
        events_total,
    }
}

/// Exact field-level kernel↔oracle comparison for every obligation id.
pub fn assert_kernel_matches_oracle(monitor: &Monitor, oracle: &BTreeMap<String, RefInstance>) {
    assert_eq!(
        monitor.obligations.len(),
        oracle.len(),
        "obligation count differs (kernel {} vs oracle {})",
        monitor.obligations.len(),
        oracle.len()
    );
    for o in &monitor.obligations {
        let r = oracle
            .get(&o.id)
            .unwrap_or_else(|| panic!("oracle missing obligation {}", o.id));
        let k_status = serde_json::to_value(o.status)
            .unwrap()
            .as_str()
            .unwrap()
            .to_string();
        let r_status = serde_json::to_value(r.status)
            .unwrap()
            .as_str()
            .unwrap()
            .to_string();
        assert_eq!(k_status, r_status, "status mismatch on {}", o.id);
        assert_eq!(
            serde_json::to_value(o.reason).unwrap().as_str().unwrap(),
            serde_json::to_value(r.reason).unwrap().as_str().unwrap(),
            "reason mismatch on {}",
            o.id
        );
        assert_eq!(
            o.trigger_step, r.trigger_step,
            "trigger_step mismatch on {}",
            o.id
        );
        assert_eq!(
            o.window_start, r.window_start,
            "window_start mismatch on {}",
            o.id
        );
        assert_eq!(
            o.window_end, r.window_end,
            "window_end mismatch on {}",
            o.id
        );
        assert_eq!(
            o.satisfied_at, r.satisfied_at,
            "satisfied_at mismatch on {}",
            o.id
        );
        assert_eq!(o.failed_at, r.failed_at, "failed_at mismatch on {}", o.id);
    }
}

pub fn oracle_open(d: &Driven) -> BTreeMap<String, RefInstance> {
    reference::evaluate_run(&d.oracle_segments, false)
}
pub fn oracle_closed(d: &Driven) -> BTreeMap<String, RefInstance> {
    reference::evaluate_run(&d.oracle_segments, true)
}

// ------------------------------------------------------------- replay log
static LOG_LOCK: Mutex<()> = Mutex::new(());

pub fn replay_log_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target")
        .join("replay")
        .join("integration.jsonl")
}

/// Append one structured line recording an intermediate state. The log is
/// enough to reconstruct the run: run id, step, the decisions at that step
/// and the global verdict with reasons.
pub fn log_line(test: &str, run_id: &str, stage: &str, payload: Value) {
    let _g = LOG_LOCK.lock().unwrap();
    let path = replay_log_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    let line = json!({
        "ts": chrono_like(),
        "test": test,
        "run_id": run_id,
        "stage": stage,
        "payload": payload,
    });
    let mut f = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .unwrap();
    writeln!(f, "{}", serde_json::to_string(&line).unwrap()).unwrap();
}

/// Monotonic millisecond timestamp (tests must not depend on wall clock, but
/// a replay log benefits from ordering).
fn chrono_like() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

/// Log every step report of a driven run.
pub fn log_run(test: &str, d: &Driven) {
    for r in &d.reports {
        log_line(
            test,
            &d.run_id,
            "step",
            json!({
                "step": r.step,
                "epoch": r.epoch,
                "version": r.version,
                "spawned": r.spawned,
                "satisfied": r.satisfied,
                "violated": r.violated,
                "global_verdict": r.verdict,
            }),
        );
    }
}
