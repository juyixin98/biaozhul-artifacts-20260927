//! Garbage collection: mark-and-sweep with arena compaction, preserving the
//! *logical identity* of every surviving node.
//!
//! Only roots explicitly handed in are retained; every node transitively
//! reachable from them is marked. The arena is then compacted (slots move),
//! but each surviving node keeps its stable logical id: edges still name the
//! same ids, and the `id -> slot` redirect table is rebuilt. Consequently:
//!
//! * a reference to a surviving (reachable) node keeps working transparently;
//! * a reference to a reclaimed logical id is rejected as
//!   [`ErrorKind::StaleReference`] rather than misread;
//! * the epoch (collecting-pass counter) advances when anything is reclaimed.

use std::collections::HashMap;

use super::error::Result;
use super::manager::{BddManager, GcReport, Node, NodeRef};

impl BddManager {
    /// Collect every node not reachable from `roots`.
    ///
    /// Roots are validated up front, so a bad/foreign reference fails the call
    /// without mutating the manager. The returned `NodeRef`s are repacked
    /// (fresh epoch) but name the same logical edges, so callers may keep
    /// using either the old or the returned handles for surviving nodes.
    pub fn gc(&mut self, roots: &[&NodeRef]) -> Result<(GcReport, Vec<NodeRef>)> {
        let root_edges: Vec<super::Edge> = roots
            .iter()
            .map(|r| self.resolve(r))
            .collect::<Result<_>>()?;
        let nodes_before = self.node_count();
        let epoch_before = self.epoch;

        // Phase 1: mark reachable slots from the roots.
        let mut live = vec![false; self.nodes.len()];
        self.mark_slot(1, &mut live)?;
        for e in &root_edges {
            self.mark_slot(self.slot_of(*e)?, &mut live)?;
        }

        // Phase 2: compact slots, keeping logical ids fixed.
        let mut new_nodes: Vec<Node> = Vec::with_capacity(self.nodes.len());
        new_nodes.push(self.nodes[0]); // unused slot 0
        new_nodes.push(self.nodes[1]); // terminal stays at slot 1
        let mut new_slot_to_id = vec![0u32, 1u32];
        // old slot -> new slot for live nodes; 0 means collected.
        let mut remap_slot: Vec<usize> = vec![0; self.nodes.len()];
        remap_slot[1] = 1;
        for old in 2..self.nodes.len() {
            if live[old] {
                remap_slot[old] = new_nodes.len();
                new_nodes.push(self.nodes[old]);
                new_slot_to_id.push(self.slot_to_id[old]);
            }
        }

        let collected = self.nodes.len() - new_nodes.len();

        // Edges name stable ids, which do not change, so node contents need no
        // rewrite — only the redirect table moves.
        self.nodes = new_nodes;
        self.slot_to_id = new_slot_to_id;

        let mut id_to_slot: HashMap<u32, usize> = HashMap::with_capacity(self.slot_to_id.len());
        for (slot, &id) in self.slot_to_id.iter().enumerate().skip(1) {
            id_to_slot.insert(id, slot);
        }
        self.id_to_slot = id_to_slot;

        // Rebuild the structural unique table from surviving nodes, keyed on
        // their stable logical ids (child ids are unchanged by compaction).
        let mut unique = HashMap::new();
        for slot in 2..self.nodes.len() {
            let n = self.nodes[slot];
            unique.insert((n.var, n.low, n.high), self.slot_to_id[slot]);
        }
        self.unique = unique;
        // Apply-cache keys are stable edges too, but their ids survive; drop
        // conservatively since interning changed slot layout.
        self.apply_cache.clear();

        if collected > 0 {
            self.epoch += 1;
        }
        self.stats.gc_passes += 1;

        let new_roots = root_edges
            .iter()
            .map(|e| NodeRef {
                manager_id: self.id,
                epoch: self.epoch,
                edge_raw: e.raw(),
            })
            .collect();

        Ok((
            GcReport {
                nodes_before,
                nodes_after: self.node_count(),
                collected,
                epoch_before,
                epoch_after: self.epoch,
            },
            new_roots,
        ))
    }

    /// Live node slots (terminal included) reachable from roots.
    pub fn reachable_count(&self, roots: &[&NodeRef]) -> Result<usize> {
        let mut seen = vec![false; self.nodes.len()];
        self.mark_slot(1, &mut seen)?;
        for r in roots {
            let e = self.resolve(r)?;
            self.mark_slot(self.slot_of(e)?, &mut seen)?;
        }
        Ok(seen.iter().filter(|&&m| m).count())
    }
}
