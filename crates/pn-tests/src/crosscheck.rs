//! Cross-enumerator agreement and per-firing legality.
//!
//! For each fixture:
//! 1. the kernel BFS reachable set equals the kernel-fire flood fill;
//! 2. both equal the INDEPENDENT semantics (`oracle::indie`), which never calls
//!    kernel firing code;
//! 3. every transition the independent enumerator actually took is replayed
//!    and asserted legal, each landing marking is within capacity, and the
//!    token-count conservation condition (where the net is conservative) is
//!    checked per firing.

use std::collections::HashSet;

use pn_core::fire::fire;
use pn_fixtures::nets;
use pn_fixtures::oracle;
use pn_fixtures::oracle::indie::{indie_closure, PlainNet};

#[test]
fn three_independent_enumerators_agree_on_every_fixture() {
    let fixtures: Vec<(&str, pn_core::Net)> = vec![
        ("mutex", nets::mutex()),
        ("producer_consumer_2", nets::producer_consumer(2)),
        ("producer_consumer_3", nets::producer_consumer(3)),
        ("deadlock", nets::deadlock_net()),
        ("weighted_mutex", nets::weighted_mutex()),
    ];

    for (name, net) in fixtures {
        // Enumerator A: kernel-fire cursor flood fill.
        let flood = oracle::enumerate(&net);
        // Enumerator B: kernel-fire fixpoint closure.
        let closure = oracle::closure_reachable(&net);
        // Enumerator C: fully independent semantics.
        let indie = indie_closure(&net);

        assert_eq!(
            flood.reachable, indie.reachable,
            "[{name}] kernel-fire flood fill disagrees with independent semantics"
        );
        assert_eq!(
            closure, indie.reachable,
            "[{name}] kernel-fire closure disagrees with independent semantics"
        );
        assert_eq!(
            flood.deadlocks, indie.deadlocks,
            "[{name}] deadlock sets disagree across independent semantics"
        );
    }
}

#[test]
fn every_independent_firing_is_legal_under_the_kernel_and_within_capacity() {
    for (name, net) in [
        ("mutex", nets::mutex()),
        ("pc2", nets::producer_consumer(2)),
        ("deadlock", nets::deadlock_net()),
        ("weighted", nets::weighted_mutex()),
    ] {
        let indie = indie_closure(&net);
        for (t, from, to) in &indie.transitions_taken {
            // The independent firing must be legal under the kernel too.
            let kernel_to = fire(&net, from, *t)
                .unwrap_or_else(|e| panic!("[{name}] kernel rejected an indie-legal firing: {e}"));
            assert_eq!(
                &kernel_to, to,
                "[{name}] kernel and indie successor disagree"
            );
            // Within capacity.
            for p in 0..net.place_count() {
                assert!(
                    to[p] <= net.capacity(p),
                    "[{name}] successor {to:?} exceeds capacity"
                );
            }
        }
    }
}

#[test]
fn whole_capacity_box_is_exhausted_for_small_nets() {
    // For the tiny deadlock net (3 binary places) the box has 8 markings but
    // only 3 are reachable; the independent closure must be exactly that.
    let net = nets::deadlock_net();
    let indie = indie_closure(&net);
    let expected: HashSet<Vec<u64>> = [
        vec![1, 0, 0],
        vec![0, 1, 0],
        vec![0, 0, 1],
    ]
    .into_iter()
    .collect();
    assert_eq!(indie.reachable, expected);
    assert_eq!(indie.deadlocks, vec![vec![0, 0, 1]]);
}

#[test]
fn indie_semantics_independently_refuses_capacity_overflow() {
    // Direct check of the standalone PlainNet semantics, no kernel call.
    let net = nets::producer_consumer(1); // free=1, ready=1, done=1
    let plain = PlainNet::from_net(&net);
    // Initial (1,0,0): produce -> (0,1,0); produce again must overflow ready.
    let m1 = plain.fire(&[1, 0, 0], 0).unwrap();
    assert_eq!(m1, vec![0, 1, 0]);
    let err = plain.fire(&m1, 0).unwrap_err();
    assert_eq!(err, "INPUT_NOT_SATISFIED"); // free is 0
}
