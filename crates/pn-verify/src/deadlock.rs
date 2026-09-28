//! Independent deadlock verification: attempt every transition.

use pn_core::fire::enabled;
use pn_core::{Marking, Net, Token};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeadlockVerdict {
    pub is_deadlock: bool,
    /// Transitions that are still enabled at the claimed marking. Empty for a
    /// genuine deadlock.
    pub enabled_transitions: Vec<String>,
    pub within_capacity: bool,
    pub reasons: Vec<String>,
}

/// A marking is a deadlock iff it respects capacities and no transition is
/// enabled. Checked without consulting the solver.
pub fn verify_deadlock(net: &Net, marking: &[Token]) -> DeadlockVerdict {
    let mut reasons = Vec::new();
    let mut within_capacity = marking.len() == net.place_count();
    if marking.len() != net.place_count() {
        reasons.push(format!(
            "marking length {} != place count {}",
            marking.len(),
            net.place_count()
        ));
    }
    for (p, &tokens) in marking.iter().enumerate() {
        if tokens > net.capacity(p) {
            within_capacity = false;
            reasons.push(format!(
                "place '{}' holds {tokens} tokens, capacity is {}",
                net.place_name(p),
                net.capacity(p)
            ));
        }
    }

    let m: Marking = marking.to_vec();
    let enabled_transitions: Vec<String> = (0..net.transition_count())
        .filter(|&t| enabled(net, &m, t))
        .map(|t| net.transition_name(t).to_string())
        .collect();

    let is_deadlock = within_capacity && enabled_transitions.is_empty();
    if !enabled_transitions.is_empty() {
        reasons.push(format!(
            "not a deadlock; enabled transitions: {enabled_transitions:?}"
        ));
    }

    DeadlockVerdict {
        is_deadlock,
        enabled_transitions,
        within_capacity,
        reasons,
    }
}
