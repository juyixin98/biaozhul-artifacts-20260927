use crate::helpers::*;
use fsm_core::QueryKind;
use fsm_fixtures::{
    answers, counter, counter_deadlock, COUNTER_INVARIANT, COUNTER_REACHES_CAP,
    COUNTER_UNREACHABLE_ERROR,
};

#[test]
fn counter_terminal_is_not_deadlock() {
    let sys = build(counter());
    let out = run_with(
        &sys,
        &[
            ("inv", QueryKind::Ag, COUNTER_INVARIANT),
            ("err_unreachable", QueryKind::Ef, COUNTER_UNREACHABLE_ERROR),
            ("cap", QueryKind::Ef, COUNTER_REACHES_CAP),
        ],
        true,
        10_000,
    );
    assert_eq!(out.status, fsm_core::RunStatus::Complete);
    assert_eq!(out.stats.states_consumed, answers::COUNTER_REACHABLE_STATES);
    assert_eq!(out.stats.terminal_states, answers::COUNTER_TERMINAL_STATES);
    assert_eq!(
        out.stats.deadlocked_states,
        answers::COUNTER_DEADLOCKED_STATES
    );

    assert_conclusion(&out, "inv", answers::COUNTER_INVARIANT_EXPECTED);
    assert_conclusion(
        &out,
        "err_unreachable",
        answers::COUNTER_UNREACHABLE_EXPECTED,
    );
    assert_conclusion(&out, "cap", answers::COUNTER_CAP_EXPECTED);
    assert_eq!(
        evidence_of(&out, "cap").length,
        answers::COUNTER_CAP_PATH_LENGTH
    );
    assert!(!out.deadlock_found);
}

#[test]
fn counter_without_terminal_clause_deadlocks_at_cap() {
    let sys = build(counter_deadlock());
    let out = run_with(&sys, &[], true, 10_000);
    assert_eq!(out.status, fsm_core::RunStatus::Complete);
    assert_eq!(
        out.stats.terminal_states,
        answers::COUNTER_DL_TERMINAL_STATES
    );
    assert_eq!(
        out.stats.deadlocked_states,
        answers::COUNTER_DL_DEADLOCKED_STATES
    );
    assert!(out.deadlock_found);
    assert_conclusion(&out, "deadlock", fsm_fixtures::answers::Expected::Violated);
    let ev = out.deadlock_evidence.as_ref().unwrap();
    assert_eq!(ev.kind, fsm_core::EvidenceKind::Deadlock);
    assert_eq!(ev.length, answers::COUNTER_DL_PATH_LENGTH);
    assert_eq!(
        ev.path.last().unwrap().state["x"].as_i64(),
        Some(3),
        "deadlock occurs at the cap x=3"
    );

    // independent verifier agrees, and distinguishes it from legal terminal
    let input = crate::mutex_tests::evidence_to_input(ev, None);
    let report = fsm_verify::verify(&sys, &input);
    assert!(report.accepted, "{:?}", report.failures);
}
