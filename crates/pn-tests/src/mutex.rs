//! Mutex (mutual exclusion) fixture: concrete reachability, path and
//! exclusion assertions.

use pn_core::explore::{explore, ExploreConfig, Target, Verdict};
use pn_verify::{verify_deadlock, verify_invariant_vector, verify_witness, StepClaim};
use pn_fixtures::nets;

#[test]
fn held_state_is_reachable_via_acquire() {
    let net = nets::mutex();
    let result = explore(
        &net,
        Some(&Target::Exact(vec![0, 1])),
        &ExploreConfig::default(),
        |_| {},
    );
    assert_eq!(result.verdict, Verdict::Reachable);
    let path = result.path.expect("reachable target must carry a witness");
    assert_eq!(path.len(), 1);
    assert_eq!(path[0].transition_name, "acquire");
    assert_eq!(path[0].marking_before, vec![1, 0]);
    assert_eq!(path[0].marking_after, vec![0, 1]);
}

#[test]
fn two_simultaneous_holders_is_unreachable() {
    let net = nets::mutex();
    // (1,1) is inside the declared capacity box but violates the mutex
    // invariant, so it must be unreachable - a stronger test than an
    // outside-box target.
    let result = explore(
        &net,
        Some(&Target::Exact(vec![1, 1])),
        &ExploreConfig::default(),
        |_| {},
    );
    assert_eq!(result.verdict, Verdict::Unreachable);
    assert!(result.path.is_none());
}

#[test]
fn mutex_has_exactly_two_reachable_states_and_no_deadlock() {
    let net = nets::mutex();
    let space = explore(&net, None, &ExploreConfig::default(), |_| {});
    assert_eq!(space.stats.discovered, 2);
    assert!(space.deadlocks.is_empty(), "mutex never deadlocks");
    // Independent deadlock re-check on the two reachable markings.
    for m in [vec![1, 0], vec![0, 1]] {
        let v = verify_deadlock(&net, &m);
        assert!(!v.is_deadlock, "marking {m:?} unexpectedly deadlocked");
        assert_eq!(v.enabled_transitions.len(), 1);
    }
}

#[test]
fn witness_round_trip_release_returns_to_idle() {
    let net = nets::mutex();
    // acquire then release: idle -> busy -> idle.
    let claims = vec![
        StepClaim { transition: "acquire".into(), expected_after: Some(vec![0, 1]) },
        StepClaim { transition: "release".into(), expected_after: Some(vec![1, 0]) },
    ];
    let verified = verify_witness(&net, &claims, Some(&[1, 0]));
    assert!(verified.valid, "failures: {:?}", verified.failures);
    assert_eq!(verified.endpoint, vec![1, 0]);
    assert_eq!(verified.steps.len(), 2);
}

#[test]
fn bogus_witness_is_rejected_with_category() {
    let net = nets::mutex();
    // Cannot release from the idle state: no token on busy.
    let claims = vec![StepClaim {
        transition: "release".into(),
        expected_after: Some(vec![2, 0]),
    }];
    let verified = verify_witness(&net, &claims, None);
    assert!(!verified.valid);
    let f = &verified.failures[0];
    assert_eq!(f.kind.as_str(), "FIRING_ILLEGAL");
    assert_eq!(f.fire_failure, Some("INPUT_NOT_SATISFIED"));
    assert_eq!(f.step, 1);
}

#[test]
fn mutex_resource_invariant_is_independently_valid() {
    let net = nets::mutex();
    // y = (1,1): idle + busy is constant 1.
    let v = verify_invariant_vector(&net, &[1, 1], &[vec![1, 0], vec![0, 1], vec![1, 1]]);
    assert!(v.valid_invariant());
    // Reachable pair preserves the sum...
    assert!(v.weighted_sums[0] == v.weighted_sums[1]);
    // ...but (1,1) also sums to 2 - showing the invariant is only a
    // necessary, not sufficient condition.
    assert_ne!(v.weighted_sums[2], 1);
}
