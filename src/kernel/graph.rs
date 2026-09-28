//! BDZ-style 3-uniform hypergraph construction and peeling.
//!
//! Each key maps to an edge of three distinct vertices in `[0, m)`.
//! Peeling repeatedly removes a degree-1 vertex together with its last
//! remaining edge; the removal order (edge, critical vertex) is the stack
//! the slot-assignment pass later consumes in reverse.

use crate::hash::vertex_hash;

/// One hyperedge: three pairwise-distinct vertex ids.
pub type Edge = [u32; 3];

/// A key whose three vertex hashes do not land on distinct vertices.
/// This attempt's seed cannot be used; the builder retries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DegenerateEdge {
    pub key_index: usize,
}

/// Peeling stopped before every edge was removed: the remaining graph
/// has a 2-core, so no perfect assignment exists for this seed.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("peeling stopped with a non-empty core: peeled {peeled} of {total} edges")]
pub struct PeelFailure {
    pub peeled: usize,
    pub total: usize,
}

/// Map every key to its edge under `seed`, rejecting degenerate edges.
pub fn build_edges(seed: u64, m: usize, keys: &[Vec<u8>]) -> Result<Vec<Edge>, DegenerateEdge> {
    debug_assert!(m == 0 || m >= 3, "need at least 3 vertices for distinct triples");
    let m64 = m as u64;
    let mut edges = Vec::with_capacity(keys.len());
    for (idx, key) in keys.iter().enumerate() {
        let v0 = (vertex_hash(seed, 0, key) % m64) as u32;
        let v1 = (vertex_hash(seed, 1, key) % m64) as u32;
        let v2 = (vertex_hash(seed, 2, key) % m64) as u32;
        if v0 == v1 || v1 == v2 || v0 == v2 {
            return Err(DegenerateEdge { key_index: idx });
        }
        edges.push([v0, v1, v2]);
    }
    Ok(edges)
}

/// Peel the graph. On success returns the removal stack as
/// `(edge_index, critical_vertex)` in peel order.
pub fn peel(edges: &[Edge], m: usize) -> Result<Vec<(usize, u32)>, PeelFailure> {
    let n = edges.len();
    let mut degree = vec![0u32; m];
    let mut adj: Vec<Vec<u32>> = vec![Vec::new(); m];
    for (i, e) in edges.iter().enumerate() {
        for &v in e {
            degree[v as usize] += 1;
            adj[v as usize].push(i as u32);
        }
    }

    let mut removed = vec![false; n];
    let mut stack: Vec<(usize, u32)> = Vec::with_capacity(n);
    let mut queue: std::collections::VecDeque<u32> = (0..m as u32)
        .filter(|&v| degree[v as usize] == 1)
        .collect();

    while let Some(v) = queue.pop_front() {
        if degree[v as usize] != 1 {
            continue; // stale queue entry
        }
        let edge_id = adj[v as usize]
            .iter()
            .copied()
            .find(|&e| !removed[e as usize])
            .expect("degree-1 vertex must have a live edge") as usize;
        removed[edge_id] = true;
        stack.push((edge_id, v));
        for &u in &edges[edge_id] {
            degree[u as usize] -= 1;
            if degree[u as usize] == 1 {
                queue.push_back(u);
            }
        }
    }

    if stack.len() == n {
        Ok(stack)
    } else {
        Err(PeelFailure {
            peeled: stack.len(),
            total: n,
        })
    }
}
