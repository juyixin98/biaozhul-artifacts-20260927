//! Silent-step (tau) closures with replayable mappings.
//!
//! For every state `s` we compute `epsilon(s)` = all states reachable from `s`
//! using zero or more internal (silent) steps. Alongside the reachability set
//! we retain BFS parent pointers so *any* claimed tau path can be reproduced as
//! a concrete sequence of transitions — this is the "replayable mapping" the
//! witness builder and the independent evidence verifier both rely on.
//!
//! Tau cycles are handled by standard BFS visited sets.

use std::collections::{HashMap, VecDeque};

use crate::model::{EdgeRef, Lts, StateId};

use crate::compiler::SILENT;

/// Per-source BFS forest entry: how a state was first reached.
#[derive(Debug, Clone, Copy)]
struct Parent {
    from: StateId,
    /// Edge id within `from`'s outgoing list.
    edge_id: u32,
}

pub struct ClosureTable<'a> {
    lts: &'a Lts,
    /// `closure[s]` is the sorted tau-closure of `s`, always including `s`.
    closure: Vec<Vec<StateId>>,
    /// Lazily-filled BFS parent forests: `forests[s][t] = Parent`.
    forests: Vec<HashMap<StateId, Parent>>,
}

impl<'a> ClosureTable<'a> {
    /// Compute closures for every state by DFS/BFS over silent edges only.
    pub fn build(lts: &'a Lts) -> Self {
        let n = lts.state_count();
        let mut closure: Vec<Vec<StateId>> = Vec::with_capacity(n);

        // Fixpoint from every source. Models are typically small relative to
        // their determinization, so an independent BFS per state is fine and
        // keeps the code obviously correct.
        for s in 0..n as StateId {
            let mut reached = vec![s];
            let mut seen = vec![false; n];
            seen[s as usize] = true;
            let mut head = 0;
            while head < reached.len() {
                let cur = reached[head];
                head += 1;
                for e in &lts.outgoing[cur as usize] {
                    if e.label == SILENT && !seen[e.target as usize] {
                        seen[e.target as usize] = true;
                        reached.push(e.target);
                    }
                }
            }
            reached.sort_unstable();
            closure.push(reached);
        }

        Self {
            lts,
            closure,
            forests: (0..n).map(|_| HashMap::new()).collect(),
        }
    }

    pub fn of(&self, s: StateId) -> &[StateId] {
        &self.closure[s as usize]
    }

    /// Can `s` reach `t` via zero or more silent steps?
    pub fn tau_reaches(&self, s: StateId, t: StateId) -> bool {
        self.closure[s as usize].binary_search(&t).is_ok()
    }

    /// Closure of a whole set (union, sorted+deduplicated).
    pub fn closure_of_set(&self, set: &[StateId]) -> Vec<StateId> {
        // All per-state closures are already sorted; merge with a seen bitmap.
        let mut seen = vec![false; self.lts.state_count()];
        for &s in set {
            for &t in self.of(s) {
                seen[t as usize] = true;
            }
        }
        seen.into_iter()
            .zip(0..)
            .filter_map(|(v, id)| v.then_some(id))
            .collect()
    }

    /// Concrete tau path `s ~> t` as edge references, or `None` if unreachable.
    /// The zero-step case returns an empty vector. BFS gives the shortest path;
    /// edges are explored in declaration order, making witnesses deterministic.
    pub fn tau_path(&mut self, s: StateId, t: StateId) -> Option<Vec<EdgeRef>> {
        if s == t {
            return Some(Vec::new());
        }
        if !self.tau_reaches(s, t) {
            return None;
        }
        // Lazily build the BFS parent forest for this source.
        if self.forests[s as usize].is_empty() {
            self.fill_forest(s);
        }
        self.walk_parents(s, t)
    }

    fn fill_forest(&mut self, s: StateId) {
        let forest = &mut self.forests[s as usize];
        let mut queue = VecDeque::from([s]);
        while let Some(cur) = queue.pop_front() {
            for (edge_id, e) in self.lts.outgoing[cur as usize]
                .iter()
                .enumerate()
                .filter(|(_, e)| e.label == SILENT)
            {
                if e.target != s && !forest.contains_key(&e.target) {
                    forest.insert(
                        e.target,
                        Parent {
                            from: cur,
                            edge_id: edge_id as u32,
                        },
                    );
                    queue.push_back(e.target);
                }
            }
        }
    }

    fn walk_parents(&self, s: StateId, t: StateId) -> Option<Vec<EdgeRef>> {
        let forest = &self.forests[s as usize];
        let mut path = Vec::new();
        let mut cur = t;
        while cur != s {
            let p = forest.get(&cur)?;
            path.push(EdgeRef {
                source: p.from,
                edge_id: p.edge_id,
            });
            cur = p.from;
        }
        path.reverse();
        Some(path)
    }
}
