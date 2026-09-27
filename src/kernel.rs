//! Peeling kernel: the graph-theoretic core shared by the real hashing edge
//! source and deterministic test edge sources.
//!
//! ## BDZ-style construction
//!
//! Each key defines a 3-uniform hyperedge `{v0, v1, v2}` over `m` vertices
//! (`m = ceil(n / load_factor)`). Construction:
//!
//! 1. **Peel.** Repeatedly choose a vertex whose incidence among *remaining*
//!    edges is exactly 1. Remove its edge and record `(edge, vertex)`. If
//!    edges remain once no such vertex exists, the hypergraph has a non-empty
//!    3-core and the attempt fails (the caller retries with a new seed).
//! 2. **Assign.** Process removed pairs in reverse. Other vertices already
//!    assigned via other edges hold their g values; unassigned vertices hold
//!    g=0 (and have their occupancy bit clear, so they never rank). Set
//!    `g[v] = (i - g[a] - g[b]) mod 3`, then mark v occupied. The selector
//!    `(g[v0] + g[v1] + g[v2]) mod 3` equals `i` for every key and selects
//!    its peel vertex, which is occupied by construction.
//!
//! Ranking counts only *occupied* vertices of each g-class, so the n peel
//! vertices biject onto slots `0..n`.

use std::collections::VecDeque;

/// Source of hyperedges for a keyset under a fixed seed.
///
/// The real implementation derives edges from [`crate::hash::edge`]; tests
/// inject scripted sources to force specific hypergraphs (e.g. a solid
/// 3-core) without weakening the hashing.
pub trait EdgeSource {
    /// Number of keys / edges.
    fn n(&self) -> usize;
    /// Number of vertices.
    fn m(&self) -> usize;
    /// The three distinct vertices of edge `i`, or `None` if this edge
    /// cannot be formed for the current seed (builder treats it as retry).
    fn edge(&self, i: usize) -> Option<[u64; 3]>;
}

/// One peeling step: edge `edge_id` removed via vertex `vertex`.
#[derive(Debug, Clone, Copy)]
pub struct PeelStep {
    pub edge_id: usize,
    pub vertex: u64,
}

/// Outcome of a peeling attempt.
#[derive(Debug, Clone)]
pub enum PeelOutcome {
    /// All edges removed; steps are in peeling order (reverse assignment).
    Success(Vec<PeelStep>),
    /// Failed because a non-empty core remained. `remaining` is the count of
    /// edges that could not be peeled (0 is impossible on this variant).
    CoreRemain { remaining: usize },
    /// The edge source could not produce an edge (`edge(i) == None`).
    EdgeUnavailable,
}

impl PeelOutcome {
    pub fn is_success(&self) -> bool {
        matches!(self, PeelOutcome::Success(_))
    }
}

/// Run the peeling phase. Queue order is FIFO starting from all initially
/// degree-1 vertices; this determinism is mirrored by the Python reference.
pub fn peel<S: EdgeSource + ?Sized>(src: &S) -> PeelOutcome {
    let n = src.n();
    let m = src.m();
    if n == 0 {
        return PeelOutcome::Success(Vec::new());
    }

    // Resolve edges up front; an unformable edge aborts the attempt.
    let mut edges: Vec<[u64; 3]> = Vec::with_capacity(n);
    for i in 0..n {
        match src.edge(i) {
            Some(e) => edges.push(e),
            None => return PeelOutcome::EdgeUnavailable,
        }
    }

    // adjacency: vertex -> edge ids currently incident (alive edges only).
    let mut adj: Vec<Vec<usize>> = (0..m).map(|_| Vec::new()).collect();
    for (id, e) in edges.iter().enumerate() {
        for &v in e {
            adj[v as usize].push(id);
        }
    }
    let mut degree: Vec<usize> = adj.iter().map(|l| l.len()).collect();
    let mut alive = vec![true; n];
    // A vertex is consumed the moment its edge is removed; this guards
    // against stale queue entries (a vertex can be enqueued when its degree
    // hits 1 and still be sitting in the queue after later decrements).
    let mut consumed = vec![false; m];

    let mut queue: VecDeque<u64> = VecDeque::new();
    for v in 0..m {
        if degree[v] == 1 {
            queue.push_back(v as u64);
        }
    }

    let mut steps: Vec<PeelStep> = Vec::with_capacity(n);
    while let Some(v) = queue.pop_front() {
        if consumed[v as usize] || degree[v as usize] != 1 {
            continue;
        }
        // Find the single alive edge incident to v.
        let eid = match adj[v as usize].iter().copied().find(|&id| alive[id]) {
            Some(id) => id,
            None => continue,
        };
        steps.push(PeelStep {
            edge_id: eid,
            vertex: v,
        });
        alive[eid] = false;
        consumed[v as usize] = true;
        for &w in &edges[eid] {
            if w == v || consumed[w as usize] {
                continue;
            }
            let w = w as usize;
            degree[w] -= 1;
            if degree[w] == 1 {
                queue.push_back(w as u64);
            }
        }
        degree[v as usize] = 0;
    }

    if steps.len() == n {
        PeelOutcome::Success(steps)
    } else {
        PeelOutcome::CoreRemain {
            remaining: n - steps.len(),
        }
    }
}

/// Result of the assignment phase.
pub struct Assignment {
    /// `g` table of length `m`; values in 0..3. Only occupied vertices are
    /// meaningful for ranking.
    pub g: Vec<u8>,
    /// Occupancy bitset (length `m`): true exactly for peel vertices.
    /// Needed because g=0 is both "unassigned" and "assigned to class 0".
    pub occupied: Vec<bool>,
}

/// Assign `g` values given a successful peeling. `edges[edge_id]` gives the
/// vertex triple of each edge.
pub fn assign(edges: &[[u64; 3]], steps: &[PeelStep], m: usize) -> Assignment {
    let mut g = vec![0u8; m];
    let mut occupied = vec![false; m];
    for step in steps.iter().rev() {
        let e = edges[step.edge_id];
        let pos = e
            .iter()
            .position(|&v| v == step.vertex)
            .expect("peel vertex belongs to its edge");
        let others: [u64; 2] = if pos == 0 {
            [e[1], e[2]]
        } else if pos == 1 {
            [e[0], e[2]]
        } else {
            [e[0], e[1]]
        };
        let sum = (g[others[0] as usize] + g[others[1] as usize]) % 3;
        g[step.vertex as usize] = (pos as u8 + 3 - sum) % 3;
        occupied[step.vertex as usize] = true;
    }
    Assignment { g, occupied }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FixedSource {
        edges: Vec<[u64; 3]>,
        m: usize,
    }
    impl EdgeSource for FixedSource {
        fn n(&self) -> usize {
            self.edges.len()
        }
        fn m(&self) -> usize {
            self.m
        }
        fn edge(&self, i: usize) -> Option<[u64; 3]> {
            Some(self.edges[i])
        }
    }

    #[test]
    fn empty_set_peels_to_empty_assignment() {
        let src = FixedSource {
            edges: vec![],
            m: 1,
        };
        match peel(&src) {
            PeelOutcome::Success(s) => assert!(s.is_empty()),
            other => panic!("expected success, got {other:?}"),
        }
    }

    #[test]
    fn isolated_edges_peel() {
        // Two disjoint edges on 6 vertices.
        let src = FixedSource {
            edges: vec![[0, 1, 2], [3, 4, 5]],
            m: 6,
        };
        let out = peel(&src);
        assert!(out.is_success());
        if let PeelOutcome::Success(steps) = out {
            assert_eq!(steps.len(), 2);
            let a = assign(&src.edges, &steps, src.m);
            assert_eq!(a.occupied.iter().filter(|o| **o).count(), 2);
            // For each edge the selector points at its own peel vertex.
            for (id, e) in src.edges.iter().enumerate() {
                let s = (a.g[e[0] as usize] + a.g[e[1] as usize] + a.g[e[2] as usize]) % 3;
                let v = e[s as usize];
                assert!(a.occupied[v as usize]);
                assert_eq!(steps.iter().find(|st| st.edge_id == id).unwrap().vertex, v);
            }
        }
    }

    #[test]
    fn solid_core_is_rejected_with_remaining_count() {
        // K4^3: all four 3-subsets of {0,1,2,3}. Every vertex has degree 3;
        // removing nothing is possible — immediate non-empty 3-core.
        let src = FixedSource {
            edges: vec![[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]],
            m: 4,
        };
        match peel(&src) {
            PeelOutcome::CoreRemain { remaining } => assert_eq!(remaining, 4),
            other => panic!("expected core, got {other:?}"),
        }
    }
}
