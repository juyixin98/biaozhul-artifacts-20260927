//! A fully independent re-implementation of the firing semantics.
//!
//! Nothing here calls a kernel firing routine. The net is copied into plain
//! `(place, weight)` tables and enabling, atomic consume/produce and capacity
//! enforcement are re-derived independently. This is the reference semantics
//! the tests cross-check the kernel against.

use std::collections::HashSet;

use pn_core::Net;

/// One transition as plain arc tables: (input arcs, output arcs), each arc a
/// `(place index, weight)` pair.
pub type TransitionTable = (Vec<(usize, u64)>, Vec<(usize, u64)>);

/// Arc table extracted from the net (data only, no kernel behaviour).
#[derive(Debug, Clone)]
pub struct PlainNet {
    pub capacities: Vec<u64>,
    pub initial: Vec<u64>,
    pub transition_names: Vec<String>,
    pub transitions: Vec<TransitionTable>,
}

impl PlainNet {
    pub fn from_net(net: &Net) -> PlainNet {
        let mut transitions: Vec<TransitionTable> = Vec::new();
        for tr in net.transitions() {
            let mut ins = Vec::new();
            for a in &tr.inputs {
                ins.push((net.place_index(&a.place).unwrap(), a.weight));
            }
            let mut outs = Vec::new();
            for a in &tr.outputs {
                outs.push((net.place_index(&a.place).unwrap(), a.weight));
            }
            transitions.push((ins, outs));
        }
        PlainNet {
            capacities: (0..net.place_count()).map(|p| net.capacity(p)).collect(),
            initial: net.initial().to_vec(),
            transition_names: (0..net.transition_count())
                .map(|t| net.transition_name(t).to_string())
                .collect(),
            transitions,
        }
    }

    /// Independent enabling test: ALL input arcs satisfied simultaneously.
    pub fn enabled(&self, m: &[u64], t: usize) -> bool {
        let (ins, _) = &self.transitions[t];
        ins.iter().all(|&(p, w)| m[p] >= w)
    }

    /// Independent atomic fire. Returns the successor marking only if every
    /// produced place stays within capacity; otherwise the category of the
    /// refusal. Capacity overflow is a refusal, never a truncation.
    pub fn fire(&self, m: &[u64], t: usize) -> Result<Vec<u64>, &'static str> {
        if !self.enabled(m, t) {
            return Err("INPUT_NOT_SATISFIED");
        }
        let (ins, outs) = &self.transitions[t];

        // Compute net change per place (inputs aggregate, outputs aggregate;
        // the construction forbids duplicate endpoints).
        let mut delta = vec![0i128; m.len()];
        for &(p, w) in ins {
            delta[p] -= w as i128;
        }
        for &(p, w) in outs {
            delta[p] += w as i128;
        }

        let mut next = m.to_vec();
        for p in 0..m.len() {
            let v = m[p] as i128 + delta[p];
            // Inputs already verified, so v >= 0.
            if v < 0 {
                return Err("INTERNAL_NEGATIVE");
            }
            if v > self.capacities[p] as i128 {
                return Err("CAPACITY_OVERFLOW");
            }
            next[p] = v as u64;
        }
        Ok(next)
    }

    pub fn has_enabled(&self, m: &[u64]) -> bool {
        (0..self.transitions.len()).any(|t| self.enabled_success(m, t))
    }

    fn enabled_success(&self, m: &[u64], t: usize) -> bool {
        self.fire(m, t).is_ok()
    }
}

/// Reference reachability result.
#[derive(Debug, Clone)]
pub struct IndieAnswer {
    pub reachable: HashSet<Vec<u64>>,
    pub deadlocks: Vec<Vec<u64>>,
    /// (transition index, from, to) for every successful firing explored.
    pub transitions_taken: Vec<(usize, Vec<u64>, Vec<u64>)>,
}

/// Fixpoint closure using the independent semantics with a growing vector.
pub fn indie_closure(net: &Net) -> IndieAnswer {
    let pn = PlainNet::from_net(net);
    let mut reachable: HashSet<Vec<u64>> = HashSet::new();
    reachable.insert(pn.initial.clone());
    let mut work: Vec<Vec<u64>> = vec![pn.initial.clone()];
    let mut taken = Vec::new();

    while let Some(m) = work.pop() {
        for t in 0..pn.transitions.len() {
            if let Ok(next) = pn.fire(&m, t) {
                taken.push((t, m.clone(), next.clone()));
                if reachable.insert(next.clone()) {
                    work.push(next);
                }
            }
        }
    }

    let mut deadlocks: Vec<Vec<u64>> = reachable
        .iter()
        .filter(|m| !pn.has_enabled(m))
        .cloned()
        .collect();
    deadlocks.sort();

    IndieAnswer {
        reachable,
        deadlocks,
        transitions_taken: taken,
    }
}
