//! Evidence verifier tests: a valid replay must verify; tampered replays must
//! be rejected with concrete problems. This proves the verifier is a real
//! independent check rather than echoing the solver.

use wtio::compiler;
use wtio::input::CheckRequest;
use wtio::solver;
use wtio::verifier;
use wtio::witness::Replay;

fn load(name: &str) -> CheckRequest {
    let bytes = std::fs::read(format!("fixtures/{name}.json")).unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

fn counterexample_replay(name: &str) -> (wtio::model::Pair, Vec<String>, Replay) {
    let req = load(name);
    let pair = compiler::compile(&req).unwrap();
    let outcome = solver::check(&pair, &req.limits()).unwrap();
    assert_eq!(outcome.verdict, solver::Verdict::NotIncluded);
    let ce = outcome.counterexample.unwrap();
    (pair, ce.trace, ce.implementation_replay)
}

#[test]
fn genuine_replay_confirms() {
    let (pair, trace, replay) = counterexample_replay("erroneous_extra_output");
    let v = verifier::verify_counterexample(&pair, &replay, &trace).unwrap();
    assert!(v.confirmed, "{:?}", v.problems);
    assert!(v.implementation_run_valid);
    assert!(v.implementation_accepts_trace);
    assert!(!v.specification_accepts_trace);
}

#[test]
fn replay_starting_from_wrong_state_fails() {
    let (pair, trace, mut replay) = counterexample_replay("erroneous_extra_output");
    replay.start_state = "i2".to_string();
    let v = verifier::verify_counterexample(&pair, &replay, &trace).unwrap();
    assert!(!v.confirmed);
    assert!(
        v.problems.iter().any(|p| p.contains("initial state")),
        "{:?}",
        v.problems
    );
}

#[test]
fn replay_with_rewritten_action_fails() {
    let (pair, trace, mut replay) = counterexample_replay("erroneous_extra_output");
    replay.hops[0].action = "y".to_string();
    let v = verifier::verify_counterexample(&pair, &replay, &trace).unwrap();
    assert!(!v.confirmed);
    assert!(
        v.problems.iter().any(|p| p.contains("claims action")),
        "{:?}",
        v.problems
    );
}

#[test]
fn replay_claiming_a_nonexistent_edge_fails() {
    let (pair, trace, mut replay) = counterexample_replay("erroneous_extra_output");
    replay.hops[0].observable_edge.edge_id = 999;
    let v = verifier::verify_counterexample(&pair, &replay, &trace).unwrap();
    assert!(!v.confirmed);
    assert!(
        v.problems.iter().any(|p| p.contains("edge_id 999")),
        "{:?}",
        v.problems
    );
}

#[test]
fn replay_mismatched_against_trace_fails() {
    let (pair, _, replay) = counterexample_replay("erroneous_extra_output");
    // Claim the replay proves a different trace than the edges actually spell.
    let v = verifier::verify_counterexample(&pair, &replay, &["z".to_string()]).unwrap();
    assert!(!v.confirmed);
    assert!(
        v.problems
            .iter()
            .any(|p| p.contains("replay spells") && p.contains("z")),
        "{:?}",
        v.problems
    );
}

#[test]
fn tau_edge_faked_as_observable_fails() {
    // In this fixture the hop uses before_tau = i0 -tau-> i1. Re-label that
    // edge reference as if it were observable.
    let (pair, trace, mut replay) = counterexample_replay("erroneous_extra_output");
    replay.hops[0].before_tau[0].action = "x".to_string();
    let v = verifier::verify_counterexample(&pair, &replay, &trace).unwrap();
    assert!(!v.confirmed);
    assert!(
        v.problems.iter().any(|p| p.contains("expected silent")),
        "{:?}",
        v.problems
    );
}

#[test]
fn independent_acceptance_answers_for_hidden_case() {
    let req = load("hidden_internal_steps_included");
    let pair = compiler::compile(&req).unwrap();
    // Implementation accepts 'a' through tau;a — independent oracle path.
    assert!(verifier::trace_accepted(&pair, false, &["a".to_string()]).unwrap());
    assert!(verifier::trace_accepted(&pair, true, &["a".to_string()]).unwrap());
    assert!(!verifier::trace_accepted(&pair, false, &["b".to_string()]).unwrap());
}
