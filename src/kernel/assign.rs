//! Slot assignment: turn a successful peel stack into the `g` array.
//!
//! Processing the peel stack in reverse, each edge's critical vertex is
//! the only one of its three vertices not yet fixed, so we can choose
//! `g[critical]` to make the edge's slot sum to any target. Targets are
//! handed out as the smallest still-free slot; since slots are never
//! freed, that is simply a monotone counter `0, 1, 2, ...`, which keeps
//! the mapping minimal (exactly `0..n`) and fully deterministic.

use super::graph::Edge;

/// Compute the `g` array (vertex -> value in `[0, n)`) from a peel stack.
pub fn assign(stack: &[(usize, u32)], edges: &[Edge], m: usize, n: usize) -> Vec<u32> {
    let n64 = n as u64;
    let mut g = vec![0u32; m];
    for (next_slot, &(edge_id, critical)) in stack.iter().rev().enumerate() {
        let e = edges[edge_id];
        let mut sum_others = 0u64;
        for &v in &e {
            if v != critical {
                sum_others += g[v as usize] as u64;
            }
        }
        let slot = (next_slot as u64) % n64.max(1);
        // g[critical] = (slot - sum_others) mod n
        g[critical as usize] = ((slot + 2 * n64 - (sum_others % n64)) % n64) as u32;
    }
    g
}
