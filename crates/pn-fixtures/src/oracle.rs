//! Independent brute-force reference oracle.
//!
//! This is written *differently* from the kernel's BFS on purpose, so that a
//! shared implementation bug cannot validate itself. It provides THREE
//! independent angles:
//!
//! * [`enumerate`] / [`closure_reachable`] build the reachable set using only
//!   the kernel firing primitives (the semantics under test), never
//!   `pn_core::explore` or the solver; the two use different algorithms
//!   (cursor flood-fill vs. fixpoint closure);
//! * [`indie`] does not call the kernel AT ALL: it copies the net's arc data
//!   into plain tables and re-implements enabling, atomic firing and capacity
//!   enforcement from scratch. Agreement of the kernel with this independent
//!   semantics is the strongest cross-check in the suite.

pub mod indie;

use std::collections::HashSet;

use pn_core::fire::{fire, FireFailure};
use pn_core::{Marking, Net};

/// Reference answer for one net, computed by the independent oracle.
#[derive(Debug, Clone)]
pub struct OracleAnswer {
    pub initial: Marking,
    pub reachable: HashSet<Marking>,
    /// Reachable markings with no enabled transition.
    pub deadlocks: Vec<Marking>,
    /// How many successful firings the enumeration performed.
    pub firings: u64,
    /// How many firing attempts were rejected and why. Capacity rejections
    /// are counted separately so tests can assert overflow is forbidden, not
    /// truncated.
    pub rejected_input: u64,
    pub rejected_capacity: u64,
}

impl OracleAnswer {
    pub fn is_reachable(&self, m: &[u64]) -> bool {
        self.reachable.contains(m)
    }
}

/// Enumerate the reachable marking set with a cursor-based flood fill.
pub fn enumerate(net: &Net) -> OracleAnswer {
    let initial: Marking = net.initial().to_vec();
    let mut reachable: HashSet<Marking> = HashSet::new();
    reachable.insert(initial.clone());
    let mut order: Vec<Marking> = vec![initial.clone()];

    let mut cursor = 0usize;
    let mut firings = 0u64;
    let mut rejected_input = 0u64;
    let mut rejected_capacity = 0u64;

    while cursor < order.len() {
        let m = order[cursor].clone();
        cursor += 1;
        for t in 0..net.transition_count() {
            match fire(net, &m, t) {
                Ok(next) => {
                    firings += 1;
                    if reachable.insert(next.clone()) {
                        order.push(next);
                    }
                }
                Err(FireFailure::InputNotSatisfied { .. }) => rejected_input += 1,
                Err(FireFailure::CapacityOverflow { .. }) => rejected_capacity += 1,
                // An arithmetic anomaly is a hard error for an oracle; surface
                // it rather than counting it as a normal rejection.
                Err(FireFailure::ArithmeticOverflow { transition, place }) => {
                    panic!("oracle arithmetic overflow at {transition}/{place}")
                }
            }
        }
    }

    let mut deadlocks: Vec<Marking> = Vec::new();
    for m in &order {
        let mut any_enabled = false;
        for t in 0..net.transition_count() {
            if fire(net, m, t).is_ok() {
                any_enabled = true;
                break;
            }
        }
        if !any_enabled {
            deadlocks.push(m.clone());
        }
    }
    deadlocks.sort();

    OracleAnswer {
        initial,
        reachable,
        deadlocks,
        firings,
        rejected_input,
        rejected_capacity,
    }
}

/// Iterate every marking in the finite capacity box with a mixed-radix
/// odometer. No graph search: purely the Cartesian product of
/// `0..=capacity_p`.
pub fn each_box_marking(net: &Net) -> BoxIter {
    BoxIter {
        caps: (0..net.place_count()).map(|p| net.capacity(p)).collect(),
        current: None,
        done: false,
    }
}

pub struct BoxIter {
    caps: Vec<u64>,
    current: Option<Vec<u64>>,
    done: bool,
}

impl Iterator for BoxIter {
    type Item = Marking;

    fn next(&mut self) -> Option<Marking> {
        if self.done {
            return None;
        }
        let m = match self.current.take() {
            None => vec![0; self.caps.len()],
            Some(mut m) => {
                // Increment the odometer.
                let mut i = 0usize;
                loop {
                    if i >= m.len() {
                        self.done = true;
                        return None;
                    }
                    if m[i] < self.caps[i] {
                        m[i] += 1;
                        break;
                    }
                    m[i] = 0;
                    i += 1;
                }
                m
            }
        };
        self.current = Some(m.clone());
        Some(m)
    }
}

/// Closure reachability computed with an explicit fixpoint over the whole
/// box: start from the initial marking, keep adding legal successors until no
/// new marking appears. Independent algorithm from both BFS and the flood
/// fill (fixpoint closure, no ordering/parent tracking at all).
pub fn closure_reachable(net: &Net) -> HashSet<Marking> {
    let mut set: HashSet<Marking> = HashSet::new();
    set.insert(net.initial().to_vec());
    loop {
        let mut added: Vec<Marking> = Vec::new();
        for m in &set {
            for t in 0..net.transition_count() {
                if let Ok(next) = fire(net, m, t) {
                    if !set.contains(&next) {
                        added.push(next);
                    }
                }
            }
        }
        if added.is_empty() {
            return set;
        }
        for m in added {
            set.insert(m);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::nets;

    #[test]
    fn flood_fill_and_closure_agree_on_all_fixtures() {
        for net in [
            nets::mutex(),
            nets::producer_consumer(2),
            nets::producer_consumer(3),
            nets::deadlock_net(),
            nets::weighted_mutex(),
        ] {
            let flood = enumerate(&net);
            let closure = closure_reachable(&net);
            assert_eq!(
                flood.reachable, closure,
                "independent enumerators disagree on net with {} places",
                net.place_count()
            );
        }
    }

    #[test]
    fn box_iterator_covers_exact_product() {
        let net = nets::deadlock_net(); // capacities all 1 -> 2^3 = 8 markings
        let all: Vec<_> = each_box_marking(&net).collect();
        assert_eq!(all.len(), 8);
        let unique: HashSet<_> = all.iter().cloned().collect();
        assert_eq!(unique.len(), 8);
    }
}
