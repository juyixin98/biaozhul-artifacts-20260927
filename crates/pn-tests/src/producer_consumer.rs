//! Bounded-buffer producer/consumer: capacity enforcement, concrete state
//! count and in-box unreachable target.

use std::collections::HashSet;

use pn_core::explore::{explore, ExploreConfig, Target, Verdict};
use pn_fixtures::nets;
use pn_fixtures::oracle::indie::indie_closure;
use pn_verify::verify_deadlock;

/// Reachability matrix for buffer size 2, derived by hand. Every marking
/// satisfies `free + ready == 2`; `done` records consumptions. The reachable
/// set is:
fn expected_reachable_b2() -> HashSet<Vec<u64>> {
    [
        vec![2, 0, 0],
        vec![1, 1, 0],
        vec![0, 2, 0],
        vec![2, 0, 1],
        vec![1, 1, 1],
        vec![0, 2, 1],
        vec![2, 0, 2],
        vec![1, 1, 2],
        vec![0, 2, 2],
    ]
    .into_iter()
    .collect()
}

#[test]
fn reachable_set_matches_hand_derived_matrix_and_indie() {
    let net = nets::producer_consumer(2);
    let indie = indie_closure(&net);
    assert_eq!(indie.reachable, expected_reachable_b2());
    assert_eq!(indie.reachable.len(), 9);
}

#[test]
fn full_buffer_blocks_production_but_allows_consumption() {
    let net = nets::producer_consumer(2);
    // (0,2,0): buffer full. produce is forbidden by input (free=0); consume
    // leads to (1,1,1).
    use pn_core::fire::fire;
    let m = vec![0, 2, 0];
    let blocked = fire(&net, &m, 0).unwrap_err();
    assert_eq!(pn_core::fire::fire_failure_name(&blocked), "INPUT_NOT_SATISFIED");
    let next = fire(&net, &m, 1).unwrap();
    assert_eq!(next, vec![1, 1, 1]);
}

#[test]
fn concrete_targets_have_expected_verdicts() {
    let net = nets::producer_consumer(2);
    let cfg = ExploreConfig::default();

    let one_consumed = explore(&net, Some(&Target::Exact(vec![2, 0, 1])), &cfg, |_| {});
    assert_eq!(one_consumed.verdict, Verdict::Reachable);
    let path = one_consumed.path.unwrap();
    assert_eq!(path.len(), 2);
    assert_eq!(path[0].transition_name, "produce");
    assert_eq!(path[1].transition_name, "consume");

    // (1,0,0) sits INSIDE the box but free+ready=1, violating free+ready=2.
    let vanished = explore(&net, Some(&Target::Exact(vec![1, 0, 0])), &cfg, |_| {});
    assert_eq!(vanished.verdict, Verdict::Unreachable);
    assert!(vanished.path.is_none());

    let full = explore(
        &net,
        Some(&Target::Exact(vec![0, 2, 0])),
        &cfg,
        |_| {},
    );
    assert_eq!(full.verdict, Verdict::Reachable);
    assert_eq!(full.path.unwrap().len(), 2);
}

#[test]
fn stalls_at_the_capacity_saturated_marking() {
    // `done` accumulates consumptions and holds only 2 tokens. Once it is
    // saturated, consume would raise done to 3 > capacity 2 and is therefore
    // forbidden (CAPACITY_OVERFLOW); with the buffer also full, produce has no
    // free slot. (0,2,2) is the unique terminal marking.
    let net = nets::producer_consumer(2);
    let space = explore(&net, None, &ExploreConfig::default(), |_| {});
    assert_eq!(space.deadlocks.len(), 1);
    assert_eq!(space.deadlocks[0].marking, vec![0, 2, 2]);

    let indie = indie_closure(&net);
    assert_eq!(indie.deadlocks, vec![vec![0, 2, 2]]);

    // At the stall both firings are illegal, for distinct reasons.
    use pn_core::fire::fire;
    let stall = vec![0u64, 2, 2];
    let produce_err = pn_core::fire::fire_failure_name(&fire(&net, &stall, 0).unwrap_err());
    let consume_err = pn_core::fire::fire_failure_name(&fire(&net, &stall, 1).unwrap_err());
    assert_eq!(produce_err, "INPUT_NOT_SATISFIED"); // no free slot
    assert_eq!(consume_err, "CAPACITY_OVERFLOW"); // done would reach 3

    // A non-saturated marking with the buffer full can still consume.
    let v = verify_deadlock(&net, &[0, 2, 0]);
    assert!(!v.is_deadlock);
    assert_eq!(v.enabled_transitions, vec!["consume".to_string()]);
}

#[test]
fn state_limit_yields_inconclusive_not_unreachable() {
    // A truncated search must report INCONCLUSIVE, never falsely unreachable.
    let net = nets::producer_consumer(2);
    let cfg = ExploreConfig::bounded(1);
    let result = explore(&net, Some(&Target::Exact(vec![2, 0, 2])), &cfg, |_| {});
    assert_eq!(result.verdict, Verdict::Inconclusive);
    assert_eq!(
        result.stop_reason,
        pn_core::explore::StopReason::StateLimit
    );
}

#[test]
fn truncated_deadlock_enumeration_is_flagged_not_hidden() {
    // Budget zero expands no state; the deadlock list must be marked
    // truncated rather than presented as the (empty) complete answer.
    let input = pn_lang::AnalysisOptions {
        max_states: Some(0),
        compute_invariants: false,
        find_deadlocks: true,
    };
    let parsed =
        pn_lang::parse_analyze_request(nets::producer_consumer_request_json().as_bytes()).unwrap();
    let outcome = pn_solver::analyze_with_config(&with_options(parsed, input), Some(0));
    assert!(
        outcome.deadlocks_truncated,
        "a budget-starved enumeration must report truncation"
    );
}

fn with_options(
    mut input: pn_lang::AnalysisInput,
    options: pn_lang::AnalysisOptions,
) -> pn_lang::AnalysisInput {
    input.options = options;
    input
}
