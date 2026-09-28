//! The atomic firing law, including the two forbidden-firing categories.
//!
//! These are asserted against the kernel primitives directly.

use pn_core::fire::{fire, fire_failure_name, FireFailure};
use pn_core::{ArcDef, Net, PlaceDef, Token, TransitionDef};

fn build(
    place_caps: &[(&str, Token)],
    transitions: Vec<TransitionDef>,
    initial: Vec<Token>,
) -> Net {
    Net::new(
        place_caps
            .iter()
            .map(|(n, c)| PlaceDef {
                name: (*n).to_string(),
                capacity: *c,
            })
            .collect(),
        transitions,
        initial,
    )
    .unwrap()
}

#[test]
fn firing_consumes_all_inputs_before_producing_outputs() {
    // t needs both a>=1 and b>=1, produces c+=1.
    let net = build(
        &[("a", 2), ("b", 2), ("c", 2)],
        vec![TransitionDef {
            name: "t".into(),
            inputs: vec![
                ArcDef { place: "a".into(), weight: 1 },
                ArcDef { place: "b".into(), weight: 1 },
            ],
            outputs: vec![ArcDef { place: "c".into(), weight: 1 }],
        }],
        vec![1, 1, 0],
    );
    let next = fire(&net, &[1, 1, 0], 0).unwrap();
    assert_eq!(next, vec![0, 0, 1]);

    // Short on exactly one input -> illegal, marking unchanged by caller.
    let err = fire(&net, &[1, 0, 0], 0).unwrap_err();
    assert_eq!(fire_failure_name(&err), "INPUT_NOT_SATISFIED");
}

#[test]
fn capacity_overflow_is_forbidden_and_never_truncated() {
    // Source transition with no inputs producing into a full place.
    let net = build(
        &[("p", 2)],
        vec![TransitionDef {
            name: "source".into(),
            inputs: vec![],
            outputs: vec![ArcDef { place: "p".into(), weight: 1 }],
        }],
        vec![2],
    );
    let err = fire(&net, &[2], 0).unwrap_err();
    assert_eq!(fire_failure_name(&err), "CAPACITY_OVERFLOW");
    match err {
        FireFailure::CapacityOverflow { resulting, capacity, .. } => {
            // It reports the would-be count (3), not a clamped 2.
            assert_eq!(resulting, 3);
            assert_eq!(capacity, 2);
        }
        other => panic!("expected capacity overflow, got {other:?}"),
    }
}

#[test]
fn weighted_consumption_requires_full_arc_weight() {
    let net = build(
        &[("a", 4), ("b", 1)],
        vec![TransitionDef {
            name: "pack".into(),
            inputs: vec![ArcDef { place: "a".into(), weight: 3 }],
            outputs: vec![ArcDef { place: "b".into(), weight: 1 }],
        }],
        vec![3, 0],
    );
    // 3 tokens needed, only 2 available: disabled.
    let err = fire(&net, &[2, 0], 0).unwrap_err();
    assert_eq!(fire_failure_name(&err), "INPUT_NOT_SATISFIED");

    assert_eq!(fire(&net, &[3, 0], 0).unwrap(), vec![0, 1]);
}

#[test]
fn self_loop_is_atomic_and_capacity_is_checked_net() {
    // t: a -> a (weight 1) and a -> b (weight 1); with a=1, cap(b)=0, the
    // transition must be forbidden even though a self-loops fine.
    let net = build(
        &[("a", 1), ("b", 0)],
        vec![TransitionDef {
            name: "t".into(),
            inputs: vec![ArcDef { place: "a".into(), weight: 1 }],
            outputs: vec![
                ArcDef { place: "a".into(), weight: 1 },
                ArcDef { place: "b".into(), weight: 1 },
            ],
        }],
        vec![1, 0],
    );
    let err = fire(&net, &[1, 0], 0).unwrap_err();
    assert_eq!(fire_failure_name(&err), "CAPACITY_OVERFLOW");
}

#[test]
fn construction_rejects_invalid_models() {
    use pn_core::CoreError;

    // Initial marking above capacity.
    let err = Net::new(
        vec![PlaceDef { name: "p".into(), capacity: 1 }],
        vec![],
        vec![2],
    )
    .unwrap_err();
    assert!(matches!(err, CoreError::InitialExceedsCapacity { .. }));

    // Arc to unknown place.
    let err = Net::new(
        vec![PlaceDef { name: "p".into(), capacity: 1 }],
        vec![TransitionDef {
            name: "t".into(),
            inputs: vec![ArcDef { place: "ghost".into(), weight: 1 }],
            outputs: vec![],
        }],
        vec![0],
    )
    .unwrap_err();
    assert!(matches!(err, CoreError::UnknownPlace { .. }));

    // Zero weight arc.
    let err = Net::new(
        vec![PlaceDef { name: "p".into(), capacity: 1 }],
        vec![TransitionDef {
            name: "t".into(),
            inputs: vec![ArcDef { place: "p".into(), weight: 0 }],
            outputs: vec![],
        }],
        vec![0],
    )
    .unwrap_err();
    assert!(matches!(err, CoreError::ZeroWeight { .. }));
}
