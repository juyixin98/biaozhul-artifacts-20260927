//! Atomic firing semantics for ordinary weighted transitions under explicit
//! place capacities.
//!
//! Enabling a transition evaluates **all** input arcs together; firing then
//! consumes and produces atomically. Producing more tokens than the receiving
//! place can hold is a forbidden firing, never a truncation.

use crate::net::{Net, Token};
use thiserror::Error;

/// A marking as passed to firing primitives: one token count per place.
pub type MarkingRef = [Token];

/// Why an attempted transition firing is not legal.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum FireFailure {
    /// At least one input arc requires more tokens than the place holds.
    #[error(
        "input arc requires {required} token(s) on '{place}' but only {available} available (transition '{transition}')"
    )]
    InputNotSatisfied {
        transition: String,
        place: String,
        required: Token,
        available: Token,
    },
    /// Inputs are satisfied but the produced marking would exceed the place's
    /// declared capacity.
    #[error(
        "firing '{transition}' would raise '{place}' to {resulting} tokens, capacity is {capacity}; firing forbidden (no truncation)"
    )]
    CapacityOverflow {
        transition: String,
        place: String,
        resulting: Token,
        capacity: Token,
    },
    /// Token arithmetic could not be evaluated. Treated as an error, not a
    /// disabled transition: callers must not silently drop it.
    #[error("token arithmetic overflowed while firing '{transition}' at '{place}'")]
    ArithmeticOverflow { transition: String, place: String },
}

/// Stable machine-readable failure category, used by tests and the API.
pub fn fire_failure_name(f: &FireFailure) -> &'static str {
    match f {
        FireFailure::InputNotSatisfied { .. } => "INPUT_NOT_SATISFIED",
        FireFailure::CapacityOverflow { .. } => "CAPACITY_OVERFLOW",
        FireFailure::ArithmeticOverflow { .. } => "ARITHMETIC_OVERFLOW",
    }
}

/// Whether transition `t` may legally fire from `marking`. Evaluates every
/// input arc and every capacity before returning true.
pub fn enabled(net: &Net, marking: &MarkingRef, t: usize) -> bool {
    fire(net, marking, t).is_ok()
}

/// Indices of every transition that may legally fire, in declaration order.
pub fn enabled_transitions(net: &Net, marking: &MarkingRef) -> Vec<usize> {
    (0..net.transition_count())
        .filter(|&t| enabled(net, marking, t))
        .collect()
}

/// Fire transition `t` atomically, returning the successor marking or the
/// precise reason firing is forbidden.
pub fn fire(net: &Net, marking: &MarkingRef, t: usize) -> Result<Vec<Token>, FireFailure> {
    let tr = &net.transitions()[t];

    // Each side lists a place at most once (enforced at construction), so
    // index by place and aggregate input/output weights there.
    let mut consumed = vec![0u64; net.place_count()];
    let mut produced = vec![0u64; net.place_count()];
    for a in &tr.inputs {
        let p = net.place_index(&a.place).expect("validated input place");
        consumed[p] = a.weight;
    }
    for a in &tr.outputs {
        let p = net.place_index(&a.place).expect("validated output place");
        produced[p] = a.weight;
    }

    // Phase 1: simultaneously evaluate ALL input arcs. Scan every arc rather
    // than short-circuiting, and report the first offending place
    // deterministically (declaration order).
    let mut shortage: Option<FireFailure> = None;
    for a in &tr.inputs {
        let p = net.place_index(&a.place).expect("validated input place");
        let available = marking[p];
        if a.weight > available {
            shortage.get_or_insert(FireFailure::InputNotSatisfied {
                transition: tr.name.clone(),
                place: a.place.clone(),
                required: a.weight,
                available,
            });
        }
    }
    if let Some(e) = shortage {
        return Err(e);
    }

    // Phase 2: atomically derive the successor and enforce capacities. Phase 1
    // guarantees `m >= in_w`, so the subtraction never underflows. The place
    // result is `residual + out_w`; if that sum overflows u64 it is strictly
    // greater than u64::MAX and hence greater than every expressible capacity,
    // so the firing is forbidden by capacity rather than truncated.
    let mut next = marking.to_vec();
    for p in 0..net.place_count() {
        let (in_w, out_w) = (consumed[p], produced[p]);
        let residual = marking[p] - in_w;
        let place_name = net.place_name(p).to_string();

        let result = match residual.checked_add(out_w) {
            Some(v) => v,
            None => {
                return Err(FireFailure::CapacityOverflow {
                    transition: tr.name.clone(),
                    place: place_name,
                    resulting: u64::MAX,
                    capacity: net.capacity(p),
                });
            }
        };

        if result > net.capacity(p) {
            return Err(FireFailure::CapacityOverflow {
                transition: tr.name.clone(),
                place: place_name,
                resulting: result,
                capacity: net.capacity(p),
            });
        }
        next[p] = result;
    }

    Ok(next)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{ArcDef, PlaceDef, TransitionDef};

    fn net_with(places: Vec<(&str, u64)>, trans: TransitionDef, init: Vec<u64>) -> Net {
        Net::new(
            places
                .into_iter()
                .map(|(n, c)| PlaceDef {
                    name: n.to_string(),
                    capacity: c,
                })
                .collect(),
            vec![trans],
            init,
        )
        .unwrap()
    }

    #[test]
    fn fires_consume_and_produce_atomically() {
        let net = net_with(
            vec![("a", 5), ("b", 5)],
            TransitionDef {
                name: "t".into(),
                inputs: vec![ArcDef {
                    place: "a".into(),
                    weight: 2,
                }],
                outputs: vec![ArcDef {
                    place: "b".into(),
                    weight: 3,
                }],
            },
            vec![2, 0],
        );
        assert_eq!(fire(&net, &[2, 0], 0).unwrap(), vec![0, 3]);
        // One token short on the input: disabled.
        assert!(!enabled(&net, &[1, 0], 0));
    }

    #[test]
    fn self_loop_consumes_and_produces_same_place() {
        let net = net_with(
            vec![("a", 2)],
            TransitionDef {
                name: "t".into(),
                inputs: vec![ArcDef {
                    place: "a".into(),
                    weight: 1,
                }],
                outputs: vec![ArcDef {
                    place: "a".into(),
                    weight: 1,
                }],
            },
            vec![1],
        );
        assert_eq!(fire(&net, &[1], 0).unwrap(), vec![1]);
        assert!(!enabled(&net, &[0], 0));
    }

    #[test]
    fn capacity_overflow_forbids_without_truncation() {
        let net = net_with(
            vec![("a", 1), ("b", 2)],
            TransitionDef {
                name: "t".into(),
                inputs: vec![],
                outputs: vec![ArcDef {
                    place: "b".into(),
                    weight: 1,
                }],
            },
            vec![0, 2],
        );
        let err = fire(&net, &[0, 2], 0).unwrap_err();
        // The marking is untouched and the failure category is exact.
        assert_eq!(fire_failure_name(&err), "CAPACITY_OVERFLOW");
        assert!(matches!(
            err,
            FireFailure::CapacityOverflow {
                resulting: 3,
                capacity: 2,
                ..
            }
        ));
    }

    #[test]
    fn all_input_arcs_must_hold() {
        let net = net_with(
            vec![("a", 5), ("b", 5)],
            TransitionDef {
                name: "t".into(),
                inputs: vec![
                    ArcDef {
                        place: "a".into(),
                        weight: 1,
                    },
                    ArcDef {
                        place: "b".into(),
                        weight: 1,
                    },
                ],
                outputs: vec![],
            },
            vec![1, 0],
        );
        let err = fire(&net, &[1, 0], 0).unwrap_err();
        assert_eq!(fire_failure_name(&err), "INPUT_NOT_SATISFIED");
    }
}
