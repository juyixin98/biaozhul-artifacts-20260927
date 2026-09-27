use crate::helpers::*;
use fsm_core::{QueryKind, RunErrorKind, RunStatus};
use fsm_fixtures::answers::BIG_EF_NEAR_TARGET;
use fsm_fixtures::{answers, big_counter, no_init};

#[test]
fn no_initial_state_is_classified_not_proven() {
    let sys = build(no_init());
    let out = run_with(&sys, &[("p", QueryKind::Ag, "x < 3")], true, 10_000);
    assert_eq!(out.status, RunStatus::Error);
    let err = out.error.as_ref().expect("error recorded");
    assert_eq!(err.kind, RunErrorKind::NoInitialState);
    assert!(out.properties.is_empty());
    assert!(out
        .trace
        .iter()
        .any(|l| l.contains("no_initial_state") || l.contains("init")));
}

#[test]
fn budget_truncation_yields_unknown_not_holds() {
    let sys = build(big_counter());
    let out = run_with(
        &sys,
        &[
            ("ag", QueryKind::Ag, "x <= 5000"),
            ("ef_far", QueryKind::Ef, "x == 5000"),
            ("ef_near", QueryKind::Ef, BIG_EF_NEAR_TARGET),
        ],
        true,
        answers::BIG_BUDGET,
    );
    assert_eq!(out.status, RunStatus::Truncated);
    assert_eq!(out.stats.states_consumed, answers::BIG_BUDGET);
    assert!(!out.stats.reachability_closed);

    // The decisive correctness rule: truncation must never be reported as a
    // proof.
    assert_conclusion(&out, "ag", answers::BIG_EXPECTED_AG);
    assert_conclusion(&out, "ef_far", answers::BIG_EXPECTED_EF_FAR);

    // But a witness already found within the explored prefix is kept.
    assert_conclusion(&out, "ef_near", answers::BIG_EF_NEAR_EXPECTED);
    assert_eq!(
        evidence_of(&out, "ef_near").length,
        answers::BIG_EF_NEAR_LENGTH
    );

    let ag = out.properties.iter().find(|p| p.name == "ag").unwrap();
    let reason = ag.reason.as_deref().unwrap_or("");
    assert!(
        reason.contains("NOT proven") || reason.contains("budget"),
        "unknown reason must explain non-proof, got: {reason}"
    );
}
