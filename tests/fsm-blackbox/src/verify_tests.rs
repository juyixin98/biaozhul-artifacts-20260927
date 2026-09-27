use crate::helpers::build;
use crate::mutex_tests::evidence_to_input;
use fsm_fixtures::{counter, mutex_bad, MUTEX_INVARIANT};
use fsm_verify::{EvidenceInput, EvidenceKindInput, FailCode};

use crate::helpers::run_with;
use fsm_core::QueryKind;

fn tamper<F: FnOnce(&mut EvidenceInput)>(mut ev: EvidenceInput, f: F) -> EvidenceInput {
    f(&mut ev);
    ev
}

#[test]
fn valid_mutex_evidence_accepted() {
    let sys = build(mutex_bad());
    let out = run_with(
        &sys,
        &[("m", QueryKind::Ag, MUTEX_INVARIANT)],
        false,
        10_000,
    );
    let ev = evidence_to_input(
        out.properties[0].evidence.as_ref().unwrap(),
        Some(MUTEX_INVARIANT),
    );
    let r = fsm_verify::verify(&sys, &ev);
    assert!(r.accepted, "{:?}", r.failures);
}

#[test]
fn tampered_initial_state_rejected() {
    let sys = build(mutex_bad());
    let out = run_with(
        &sys,
        &[("m", QueryKind::Ag, MUTEX_INVARIANT)],
        false,
        10_000,
    );
    let ev = evidence_to_input(
        out.properties[0].evidence.as_ref().unwrap(),
        Some(MUTEX_INVARIANT),
    );
    let ev = tamper(ev, |e| {
        e.path[0]
            .state
            .insert("in1".into(), serde_json::json!(true));
    });
    let r = fsm_verify::verify(&sys, &ev);
    assert!(!r.accepted);
    assert!(r
        .failures
        .iter()
        .any(|f| f.code == FailCode::InitialStateNotSelected));
}

#[test]
fn skipped_transition_edge_rejected() {
    let sys = build(mutex_bad());
    let out = run_with(
        &sys,
        &[("m", QueryKind::Ag, MUTEX_INVARIANT)],
        false,
        10_000,
    );
    let mut ev = evidence_to_input(
        out.properties[0].evidence.as_ref().unwrap(),
        Some(MUTEX_INVARIANT),
    );
    // claim the violating state is reached directly from init
    ev.path.remove(1);
    ev.length = Some(ev.path.len() - 1);
    let r = fsm_verify::verify(&sys, &ev);
    assert!(!r.accepted);
    assert!(
        r.failures
            .iter()
            .any(|f| matches!(f.code, FailCode::GuardDisabled | FailCode::UpdateMismatch)),
        "expected edge replay failure, got {:?}",
        r.failures
    );
}

#[test]
fn unknown_transition_name_rejected() {
    let sys = build(mutex_bad());
    let out = run_with(
        &sys,
        &[("m", QueryKind::Ag, MUTEX_INVARIANT)],
        false,
        10_000,
    );
    let ev = evidence_to_input(
        out.properties[0].evidence.as_ref().unwrap(),
        Some(MUTEX_INVARIANT),
    );
    let ev = tamper(ev, |e| {
        e.path[1].fired = Some("does_not_exist".into());
    });
    let r = fsm_verify::verify(&sys, &ev);
    assert!(r
        .failures
        .iter()
        .any(|f| f.code == FailCode::UnknownTransition));
}

#[test]
fn final_state_not_actually_violating_rejected() {
    let sys = build(mutex_bad());
    let out = run_with(
        &sys,
        &[("m", QueryKind::Ag, MUTEX_INVARIANT)],
        false,
        10_000,
    );
    let ev = evidence_to_input(
        out.properties[0].evidence.as_ref().unwrap(),
        Some(MUTEX_INVARIANT),
    );
    // Claim final state has in2=false (so the invariant actually holds).
    let ev = tamper(ev, |e| {
        let last = e.path.last_mut().unwrap();
        last.state.insert("in2".into(), serde_json::json!(false));
        // also need a reachable edge: this will mismatch update, but the
        // final-invariant check must fire too
    });
    let r = fsm_verify::verify(&sys, &ev);
    assert!(!r.accepted);
    assert!(r
        .failures
        .iter()
        .any(|f| f.code == FailCode::FinalInvariantHolds || f.code == FailCode::UpdateMismatch));
}

#[test]
fn length_field_lie_rejected() {
    let sys = build(mutex_bad());
    let out = run_with(
        &sys,
        &[("m", QueryKind::Ag, MUTEX_INVARIANT)],
        false,
        10_000,
    );
    let ev = evidence_to_input(
        out.properties[0].evidence.as_ref().unwrap(),
        Some(MUTEX_INVARIANT),
    );
    let ev = tamper(ev, |e| e.length = Some(99));
    let r = fsm_verify::verify(&sys, &ev);
    assert!(r
        .failures
        .iter()
        .any(|f| f.code == FailCode::LengthFieldMismatch));
}

#[test]
fn terminal_state_is_not_accepted_as_deadlock() {
    // Build a witness against the *terminal* counter: the verifier must
    // reject it as FinalTerminal (legal stop, not deadlock). Valid full path
    // 0->1->2->3 via `inc`; x=3 is terminal and has no enabled transition.
    let sys = build(counter());
    let states = [0, 1, 2, 3];
    let fired = [None, Some("inc"), Some("inc"), Some("inc")];
    let path = states
        .iter()
        .zip(fired)
        .map(|(x, f)| fsm_verify::StepInput {
            state: serde_json::json!({"x": x}).as_object().unwrap().clone(),
            fired: f.map(String::from),
        })
        .collect();
    let ev = EvidenceInput {
        kind: EvidenceKindInput::Deadlock,
        expr: None,
        length: Some(3),
        path,
    };
    let r = fsm_verify::verify(&sys, &ev);
    assert!(!r.accepted);
    assert!(
        r.failures.iter().any(|f| f.code == FailCode::FinalTerminal),
        "terminal state must not count as deadlock: {:?}",
        r.failures
    );
}
