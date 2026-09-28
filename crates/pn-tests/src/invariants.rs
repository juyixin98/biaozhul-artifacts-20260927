//! P-invariant candidate generation and independent verification.
//!
//! The Farkas generator is cross-checked against `pn-verify`, which recomputes
//! `y^T C = 0` directly from the arcs. The generator therefore cannot
//! self-certify a bad candidate.

use pn_fixtures::nets;
use pn_solver::incidence::incidence_matrix;
use pn_solver::invariant::farkas_p_invariants;
use pn_verify::verify_invariant_vector;

#[test]
fn every_generated_candidate_is_independently_an_invariant() {
    for net in [
        nets::mutex(),
        nets::producer_consumer(2),
        nets::deadlock_net(),
        nets::weighted_mutex(),
    ] {
        let c = incidence_matrix(&net);
        let res = farkas_p_invariants(&c);
        assert!(!res.truncated, "small fixtures must not truncate");
        assert!(
            !res.candidates.is_empty(),
            "every fixture net admits at least the trivial/structural invariants"
        );
        for cand in &res.candidates {
            let v = verify_invariant_vector(&net, &cand.weights, &[]);
            assert!(
                v.valid_invariant(),
                "generated candidate {:?} fails independent verification: {:?}",
                cand.weights,
                v.reasons
            );
        }
    }
}

#[test]
fn mutex_generator_finds_resource_invariant() {
    let net = nets::mutex();
    let c = incidence_matrix(&net);
    let res = farkas_p_invariants(&c);
    assert!(res.candidates.iter().any(|i| i.weights == vec![1, 1]));
}

#[test]
fn weighted_mutex_invariant_reflects_arc_weights() {
    let net = nets::weighted_mutex();
    let c = incidence_matrix(&net);
    let res = farkas_p_invariants(&c);
    // pack consumes 2 a -> 1 b, so a + 2*b is preserved (= 2).
    assert!(res
        .candidates
        .iter()
        .any(|i| i.weights == vec![1, 2]));

    // Necessary-condition check: initial (2,0) and packed (0,1) share sum 2.
    let v = verify_invariant_vector(&net, &[1, 2], &[vec![2, 0], vec![0, 1]]);
    assert!(v.valid_invariant());
    assert!(v.equal_on_markings);
    assert_eq!(v.weighted_sums, vec![2, 2]);
}

#[test]
fn invariant_distinguishes_reachable_from_in_box_target() {
    // mutex: (1,1) has weighted sum 2 under (1,1), initial sums to 1 =>
    // a sound unreachability certificate produced by the independent verifier.
    let net = nets::mutex();
    let v = verify_invariant_vector(&net, &[1, 1], &[vec![1, 0], vec![1, 1]]);
    assert!(v.valid_invariant());
    assert!(!v.equal_on_markings);
    assert_eq!(v.weighted_sums, vec![1, 2]);
}

#[test]
fn non_invariant_vector_is_rejected_independently() {
    // (1,0) is not a mutex invariant: acquire changes idle 1 -> 0.
    let net = nets::mutex();
    let v = verify_invariant_vector(&net, &[1, 0], &[]);
    assert!(!v.is_p_invariant);
    assert_eq!(v.weighted_change, vec![-1, 1]);
}
