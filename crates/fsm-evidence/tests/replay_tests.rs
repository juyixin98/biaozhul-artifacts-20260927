//! Independent evidence-replay tests.
//!
//! Valid kernel-produced traces must replay; deliberate tampering must be
//! rejected with the specific failure category. The replayer contains no
//! BFS, so these checks do not validate the kernel using the kernel.

mod common;

use common::*;
use fsm_core::{explore, Budget};
use fsm_evidence::{replay, ReplayReport};
use fsm_lang::evidence::{kind, Evidence, NamedValue, TraceStep};
use fsm_lang::model::Value;

fn expect_valid(report: &ReplayReport) {
    assert!(report.valid, "replay failed: {:?}", report.failure);
}

#[test]
fn mutex_counterexample_replays_step_by_step() {
    let spec = load_fixture("mutex_bad");
    let outcome = explore(&spec, &Budget::default());
    let ev = &outcome.properties[0].evidence.clone().unwrap();

    let report = replay(&spec, ev);
    expect_valid(&report);
    assert_eq!(report.steps.len(), 5);
    // Each non-root step carries an independently derived check note.
    assert!(report.steps[0].check.contains("initial"));
    assert!(report.steps[1].check.contains("request0"));
    assert!(report.steps[4].check.contains("AG violation"));
}

#[test]
fn counter_witness_and_terminal_trace_replay() {
    let spec = load_fixture("counter");
    let outcome = explore(&spec, &Budget::default());
    let ef = outcome
        .properties
        .iter()
        .find(|p| p.name == "can_reach_top")
        .unwrap();
    expect_valid(&replay(&spec, ef.evidence.as_ref().unwrap()));
    assert_eq!(outcome.terminal_evidence.len(), 1);
    expect_valid(&replay(&spec, &outcome.terminal_evidence[0]));
}

#[test]
fn deadlock_trace_replays_and_is_distinguished_from_terminal() {
    let spec = load_fixture("deadlock");
    let outcome = explore(&spec, &spec_budget(&spec));
    let dl = &outcome.deadlock_evidence[0];
    expect_valid(&replay(&spec, dl));

    // Claim the same trace is a legal termination: must fail with
    // FINAL_NOT_TERMINAL because no terminal predicate exists.
    let mut fake = dl.clone();
    fake.kind = kind::TERMINAL.to_string();
    let report = replay(&spec, &fake);
    assert!(!report.valid);
    assert_eq!(report.failure.unwrap().code, "FINAL_NOT_TERMINAL");
}

#[test]
fn counter_terminal_is_rejected_when_claimed_as_deadlock() {
    let spec = load_fixture("counter");
    let outcome = explore(&spec, &Budget::default());
    let mut term = outcome.terminal_evidence[0].clone();
    term.kind = kind::DEADLOCK.to_string();
    let report = replay(&spec, &term);
    assert!(!report.valid);
    assert_eq!(report.failure.unwrap().code, "FINAL_NOT_DEADLOCK");
}

#[test]
fn tampered_final_state_is_detected() {
    let spec = load_fixture("mutex_bad");
    let outcome = explore(&spec, &Budget::default());
    let mut ev = outcome.properties[0].evidence.clone().unwrap();
    let last = ev.trace.len() - 1;
    // Flip in_cs1 back to false in the final state: the claimed final state
    // no longer follows from enter1, and also is no longer a violation.
    for nv in &mut ev.trace[last].state {
        if nv.var == "in_cs1" {
            nv.value = Value::Bool(false);
        }
    }
    let report = replay(&spec, &ev);
    assert!(!report.valid);
    assert_eq!(report.failure.unwrap().code, "UPDATE_MISMATCH");
}

#[test]
fn fabricated_transition_name_is_detected() {
    let spec = load_fixture("mutex_bad");
    let outcome = explore(&spec, &Budget::default());
    let mut ev = outcome.properties[0].evidence.clone().unwrap();
    ev.trace[1].fired = Some("teleport".into());
    let report = replay(&spec, &ev);
    assert!(!report.valid);
    assert_eq!(report.failure.unwrap().code, "UNKNOWN_TRANSITION");
}

#[test]
fn replaying_a_step_whose_guard_was_false_fails() {
    let spec = load_fixture("mutex_bad");
    let outcome = explore(&spec, &Budget::default());
    let mut ev = outcome.properties[0].evidence.clone().unwrap();
    // enter1 at step 4 is legitimately enabled; swap its fired label to
    // enter0 (whose guard is also true but whose update is different) and
    // also use a known guard-false construction: take the real trace step1
    // (request0) and relabel to leave0, whose guard needs in_cs0=true.
    ev.trace[1].fired = Some("leave0".into());
    let report = replay(&spec, &ev);
    assert!(!report.valid);
    assert_eq!(report.failure.unwrap().code, "GUARD_FALSE");
}

#[test]
fn non_initial_root_is_detected() {
    let spec = load_fixture("mutex_bad");
    let outcome = explore(&spec, &Budget::default());
    let mut ev = outcome.properties[0].evidence.clone().unwrap();
    for nv in &mut ev.trace[0].state {
        if nv.var == "wants0" {
            nv.value = Value::Bool(true);
        }
    }
    let report = replay(&spec, &ev);
    assert!(!report.valid);
    assert_eq!(report.failure.unwrap().code, "ROOT_NOT_INITIAL");
}

#[test]
fn out_of_domain_trace_value_is_detected() {
    let spec = load_fixture("counter");
    let outcome = explore(&spec, &Budget::default());
    let ef = outcome
        .properties
        .iter()
        .find(|p| p.name == "can_reach_top")
        .unwrap();
    let mut ev = ef.evidence.clone().unwrap();
    let last = ev.trace.len() - 1;
    for nv in &mut ev.trace[last].state {
        if nv.var == "c" {
            nv.value = Value::Int(99);
        }
    }
    let report = replay(&spec, &ev);
    assert!(!report.valid);
    assert_eq!(report.failure.unwrap().code, "BAD_STATE");
}

#[test]
fn empty_trace_is_rejected() {
    let spec = load_fixture("counter");
    let ev = Evidence {
        kind: kind::EF_WITNESS.into(),
        property: "can_reach_top".into(),
        trace: vec![],
    };
    let report = replay(&spec, &ev);
    assert!(!report.valid);
    assert_eq!(report.failure.unwrap().code, "EMPTY_TRACE");
}

#[test]
fn unknown_property_in_evidence_is_detected() {
    let spec = load_fixture("counter");
    let outcome = explore(&spec, &Budget::default());
    let mut ev = outcome
        .properties
        .iter()
        .find(|p| p.name == "can_reach_top")
        .unwrap()
        .evidence
        .clone()
        .unwrap();
    ev.property = "ghost_property".into();
    let report = replay(&spec, &ev);
    assert!(!report.valid);
    assert_eq!(report.failure.unwrap().code, "PROPERTY_NOT_FOUND");
}

#[test]
fn hand_built_witness_trace_replays_independently_of_kernel() {
    // Build a witness for (a=0,b=1) in the swap fixture by hand. This is a
    // fully independent expected answer, not extracted from kernel output.
    let spec = load_fixture("swap");
    let root = TraceStep {
        index: 0,
        state: vec![
            NamedValue {
                var: "a".into(),
                value: Value::Int(1),
            },
            NamedValue {
                var: "b".into(),
                value: Value::Int(0),
            },
        ],
        fired: None,
    };
    let after = TraceStep {
        index: 1,
        state: vec![
            NamedValue {
                var: "a".into(),
                value: Value::Int(0),
            },
            NamedValue {
                var: "b".into(),
                value: Value::Int(1),
            },
        ],
        fired: Some("swap".into()),
    };
    let ev = Evidence {
        kind: kind::EF_WITNESS.into(),
        property: "swapped_reachable".into(),
        trace: vec![root, after],
    };
    expect_valid(&replay(&spec, &ev));
}
