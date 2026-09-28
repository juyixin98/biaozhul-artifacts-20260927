//! Deadlock net: a terminal marking reachable by a specific sequence.

use pn_core::explore::{explore, ExploreConfig, Target, Verdict};
use pn_verify::verify_deadlock;
use pn_fixtures::nets;
use pn_fixtures::oracle::indie::indie_closure;

#[test]
fn terminal_marking_is_reachable_in_exactly_two_steps() {
    let net = nets::deadlock_net();
    let result = explore(
        &net,
        Some(&Target::Exact(vec![0, 0, 1])),
        &ExploreConfig::default(),
        |_| {},
    );
    assert_eq!(result.verdict, Verdict::Reachable);
    let path = result.path.unwrap();
    assert_eq!(path.len(), 2);
    assert_eq!(path[0].transition_name, "t1");
    assert_eq!(path[0].marking_after, vec![0, 1, 0]);
    assert_eq!(path[1].transition_name, "t2");
    assert_eq!(path[1].marking_after, vec![0, 0, 1]);
}

#[test]
fn exactly_one_deadlock_independently_confirmed() {
    let net = nets::deadlock_net();
    let space = explore(&net, None, &ExploreConfig::default(), |_| {});
    assert_eq!(space.deadlocks.len(), 1);
    assert_eq!(space.deadlocks[0].marking, vec![0, 0, 1]);
    assert_eq!(space.deadlocks[0].distance, 2);

    // Independent replay semantics agrees.
    let indie = indie_closure(&net);
    assert_eq!(indie.deadlocks, vec![vec![0, 0, 1]]);

    // And the standalone verifier, which attempts every transition itself.
    let v = verify_deadlock(&net, &[0, 0, 1]);
    assert!(v.is_deadlock);
    assert!(v.enabled_transitions.is_empty());
    assert!(v.within_capacity);

    // A non-terminal marking is not falsely reported as a deadlock.
    let v2 = verify_deadlock(&net, &[0, 1, 0]);
    assert!(!v2.is_deadlock);
    assert_eq!(v2.enabled_transitions, vec!["t2".to_string()]);
}

#[test]
fn a_marking_outside_the_reachable_three_is_unreachable() {
    let net = nets::deadlock_net();
    // (1,0,1) is in the box (capacities all 1) but unreachable: it would
    // require two tokens from a one-token linear net.
    let result = explore(
        &net,
        Some(&Target::Exact(vec![1, 0, 1])),
        &ExploreConfig::default(),
        |_| {},
    );
    assert_eq!(result.verdict, Verdict::Unreachable);
}

#[test]
fn deadlock_verifier_flags_out_of_capacity_claims() {
    let net = nets::deadlock_net();
    let v = verify_deadlock(&net, &[0, 0, 5]);
    assert!(!v.is_deadlock);
    assert!(!v.within_capacity);
}
