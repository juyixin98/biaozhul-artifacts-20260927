//! The ROBDD solver kernel.
//!
//! A [`BddManager`] owns:
//!
//! * a fixed variable order (indices 0..n, earlier = nearer to a root);
//! * a compact node arena (dense slots) holding the single terminal and
//!   deduplicated decision nodes;
//! * stable **logical node ids**: edges and external references name an id,
//!   never a slot, and a redirect table (`id -> slot`) survives arena
//!   compaction during garbage collection;
//! * a unique table on the canonical triple `(var, low, high)` (low always
//!   uncomplemented), enforcing both ROBDD reduction rules — redundant tests
//!   collapse and isostructural subfunctions share a node;
//! * a memoized apply cache and statistics.
//!
//! External handles are [`NodeRef`]s carrying the manager id and a stable
//! logical edge. Use on another manager is rejected (`foreign-manager`); an
//! edge whose logical node was actually reclaimed is rejected
//! (`stale-reference`). References to nodes that merely moved slots during
//! collection keep working via the redirect table — collection preserves every
//! reference that is still semantically live.

use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicU64, Ordering};

use crate::kernel::edge::{Edge, TERMINAL_ID};
use crate::kernel::error::{ErrorKind, KernelError, Result};
use crate::lang::Expr;

/// Variable identity: a position in the manager's fixed order (0 = first).
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]
pub struct VarId(pub u32);

/// A decision node or the terminal, stored in a dense arena slot.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) struct Node {
    pub var: u32,
    /// Edges name stable logical ids.
    pub low: Edge,
    pub high: Edge,
}

const TERMINAL_VAR: u32 = u32::MAX;
const TERMINAL: Node = Node {
    var: TERMINAL_VAR,
    low: Edge::FALSE,
    high: Edge::FALSE,
};

/// Binary Boolean operation supported by [`BddManager::apply`].
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum Op {
    And,
    Or,
    Xor,
    Implies,
    Equiv,
}

impl Op {
    /// Independent truth table of the connective itself.
    pub fn eval(self, a: bool, b: bool) -> bool {
        match self {
            Op::And => a && b,
            Op::Or => a || b,
            Op::Xor => a ^ b,
            Op::Implies => !a || b,
            Op::Equiv => a == b,
        }
    }

    fn commutative(self) -> bool {
        !matches!(self, Op::Implies)
    }
}

/// External, validated handle to a Boolean function inside one manager.
///
/// Opaque to clients; its logical edge is stable across arena compaction.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug, serde::Serialize, serde::Deserialize)]
pub struct NodeRef {
    pub(crate) manager_id: u64,
    /// Number of collecting GC passes observed at issue (informational).
    pub(crate) epoch: u64,
    pub(crate) edge_raw: u32,
}

impl NodeRef {
    /// Id of the issuing manager.
    pub fn manager_id(&self) -> u64 {
        self.manager_id
    }
    /// GC epoch at issue time.
    pub fn epoch(&self) -> u64 {
        self.epoch
    }
    /// Packed logical edge, meaningful only inside the issuing manager.
    pub fn edge_raw(&self) -> u32 {
        self.edge_raw
    }
}

/// Summary returned by a garbage collection pass.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GcReport {
    pub nodes_before: usize,
    pub nodes_after: usize,
    pub collected: usize,
    pub epoch_before: u64,
    pub epoch_after: u64,
}

/// Mutable statistics counters.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    pub mk_calls: u64,
    pub redundant_eliminated: u64,
    pub unique_hits: u64,
    pub apply_calls: u64,
    pub apply_cache_hits: u64,
    pub restrict_calls: u64,
    pub gc_passes: u64,
}

static NEXT_MANAGER_ID: AtomicU64 = AtomicU64::new(1);

/// The reduced ordered BDD manager.
pub struct BddManager {
    pub(crate) id: u64,
    pub(crate) epoch: u64,
    pub(crate) var_names: Vec<String>,
    pub(crate) index_of: HashMap<String, u32>,
    /// Dense arena; slot 0 unused, slot 1 terminal.
    pub(crate) nodes: Vec<Node>,
    /// Logical id of the node currently in each slot (len == nodes.len()).
    pub(crate) slot_to_id: Vec<u32>,
    /// Logical id -> current slot. Only contains live (non-collected) ids.
    pub(crate) id_to_slot: HashMap<u32, usize>,
    /// Next fresh logical id to allocate.
    pub(crate) next_id: u32,
    /// Canonical structural triple `(var, low, high)` (low uncomplemented)
    /// -> the single canonical logical id realizing it. Inductively, every
    /// child edge is terminal or a unique-table result, so structurally equal
    /// subfunctions always carry equal edge ids and one triple means one node.
    pub(crate) unique: HashMap<(u32, Edge, Edge), u32>,
    pub(crate) apply_cache: HashMap<(Op, Edge, Edge), Edge>,
    pub(crate) stats: Stats,
}

// ---------------------------------------------------------------------------
// Construction / introspection
// ---------------------------------------------------------------------------

impl BddManager {
    /// Create a manager with `order` the fixed variable order. Duplicate
    /// names are rejected.
    pub fn new(order: impl IntoIterator<Item = impl Into<String>>) -> Result<Self> {
        let var_names: Vec<String> = order.into_iter().map(Into::into).collect();
        let mut index_of = HashMap::new();
        for (i, name) in var_names.iter().enumerate() {
            if index_of.insert(name.clone(), i as u32).is_some() {
                return Err(KernelError::new(
                    ErrorKind::InvalidOrder,
                    format!("duplicate variable {name:?} in order"),
                ));
            }
        }
        Ok(BddManager {
            id: NEXT_MANAGER_ID.fetch_add(1, Ordering::Relaxed),
            epoch: 0,
            var_names,
            index_of,
            nodes: vec![TERMINAL, TERMINAL],
            slot_to_id: vec![0, TERMINAL_ID],
            id_to_slot: HashMap::from([(TERMINAL_ID, 1)]),
            next_id: TERMINAL_ID + 1,
            unique: HashMap::new(),
            apply_cache: HashMap::new(),
            stats: Stats::default(),
        })
    }

    pub fn id(&self) -> u64 {
        self.id
    }

    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    pub fn order(&self) -> &[String] {
        &self.var_names
    }

    pub fn var_id(&self, name: &str) -> Result<VarId> {
        self.index_of.get(name).copied().map(VarId).ok_or_else(|| {
            KernelError::new(
                ErrorKind::UnknownVariable,
                format!("variable {name:?} is not declared in this manager"),
            )
        })
    }

    pub fn var_name(&self, id: VarId) -> Option<&str> {
        self.var_names.get(id.0 as usize).map(String::as_str)
    }

    /// Live arena slots (terminal included).
    pub fn node_count(&self) -> usize {
        self.nodes.len() - 1
    }

    /// Live internal (non-terminal) decision nodes.
    pub fn internal_count(&self) -> usize {
        self.nodes.len() - 2
    }

    /// Internal decision nodes reachable from one function root.
    pub fn reachable_internal_count(&self, root: &NodeRef) -> Result<usize> {
        let e = self.resolve(root)?;
        let slot = self.slot_of(e)?;
        let mut seen = vec![false; self.nodes.len()];
        self.mark_slot(slot, &mut seen)?;
        Ok(seen.iter().skip(2).filter(|&&m| m).count())
    }

    pub fn stats(&self) -> &Stats {
        &self.stats
    }

    // -- reference handling -------------------------------------------------

    fn wrap(&self, e: Edge) -> NodeRef {
        NodeRef {
            manager_id: self.id,
            epoch: self.epoch,
            edge_raw: e.raw(),
        }
    }

    /// Validate manager provenance and resolve a stable edge to a live node.
    pub(crate) fn resolve(&self, r: &NodeRef) -> Result<Edge> {
        if r.manager_id != self.id {
            return Err(KernelError::new(
                ErrorKind::ForeignManager,
                format!(
                    "reference belongs to manager {} but was used with manager {}",
                    r.manager_id, self.id
                ),
            ));
        }
        let e = Edge::from_raw(r.edge_raw);
        if e.id() == 0 {
            // Logical id 0 is never allocated: malformed/forged encoding.
            return Err(KernelError::new(
                ErrorKind::InvalidNode,
                "packed edge names logical node 0, which never exists".to_string(),
            ));
        }
        // Redirect through the logical-id table; a missing id was reclaimed.
        if self.id_to_slot.contains_key(&e.id()) {
            Ok(e)
        } else {
            Err(KernelError::new(
                ErrorKind::StaleReference,
                format!(
                    "reference to logical node {} is stale: that node was reclaimed (current epoch {})",
                    e.id(),
                    self.epoch
                ),
            ))
        }
    }

    /// Current slot of an edge's logical node (edge must be live).
    pub(crate) fn slot_of(&self, e: Edge) -> Result<usize> {
        self.id_to_slot.get(&e.id()).copied().ok_or_else(|| {
            KernelError::new(
                ErrorKind::StaleReference,
                format!("logical node {} has been reclaimed", e.id()),
            )
        })
    }

    fn is_terminal_edge(&self, e: Edge) -> bool {
        e.id() == TERMINAL_ID
    }

    // -- node construction --------------------------------------------------

    /// Canonical constructor enforcing the two reduction rules.
    fn mk(&mut self, var: VarId, low: Edge, high: Edge) -> Edge {
        self.stats.mk_calls += 1;

        // Rule 1: redundant test.
        if low == high {
            self.stats.redundant_eliminated += 1;
            return low;
        }

        // Canonicalize a complemented *low* edge onto the node: the stored
        // low edge is always uncomplemented (low-regular convention).
        let (low, high, node_comp) = normalize(low, high);

        // Rule 2: unique-table interning on canonical stable edges.
        if let Some(&id) = self.unique.get(&(var.0, low, high)) {
            self.stats.unique_hits += 1;
            return Edge::new(id, node_comp);
        }

        let id = self.next_id;
        self.next_id += 1;
        let slot = self.nodes.len();
        self.nodes.push(Node {
            var: var.0,
            low,
            high,
        });
        self.slot_to_id.push(id);
        self.id_to_slot.insert(id, slot);
        self.unique.insert((var.0, low, high), id);
        Edge::new(id, node_comp)
    }
}

/// Push parity from a complemented low edge onto the outgoing edge.
fn normalize(low: Edge, high: Edge) -> (Edge, Edge, bool) {
    if low.comp() {
        (low.negate(), high.negate(), true)
    } else {
        (low, high, false)
    }
}

// ---------------------------------------------------------------------------
// Build / apply / restrict / evaluate
// ---------------------------------------------------------------------------

impl BddManager {
    /// Build the canonical ROBDD for an [`Expr`].
    pub fn build(&mut self, expr: &Expr) -> Result<NodeRef> {
        let e = self.build_expr(expr)?;
        Ok(self.wrap(e))
    }

    fn build_expr(&mut self, expr: &Expr) -> Result<Edge> {
        match expr {
            Expr::Const(b) => Ok(if *b { Edge::TRUE } else { Edge::FALSE }),
            Expr::Var(name) => {
                let v = self.var_id(name)?;
                Ok(self.mk(v, Edge::FALSE, Edge::TRUE))
            }
            Expr::Not(inner) => Ok(self.build_expr(inner)?.negate()),
            Expr::Binary { op, lhs, rhs } => {
                let a = self.build_expr(lhs)?;
                let b = self.build_expr(rhs)?;
                Ok(self.apply(op.kernel_op(), a, b))
            }
        }
    }

    /// Apply a connective to two functions of this manager.
    pub fn apply_op(&mut self, op: Op, a: &NodeRef, b: &NodeRef) -> Result<NodeRef> {
        let ea = self.resolve(a)?;
        let eb = self.resolve(b)?;
        let edge = self.apply(op, ea, eb);
        Ok(self.wrap(edge))
    }

    /// Negate a function (free parity flip; creates no nodes).
    pub fn not(&self, a: &NodeRef) -> Result<NodeRef> {
        let e = self.resolve(a)?;
        Ok(self.wrap(e.negate()))
    }

    fn apply(&mut self, op: Op, a: Edge, b: Edge) -> Edge {
        if self.is_terminal_edge(a) && self.is_terminal_edge(b) {
            return if op.eval(a.terminal_value(), b.terminal_value()) {
                Edge::TRUE
            } else {
                Edge::FALSE
            };
        }

        let key = cache_key(op, a, b);
        if let Some(&r) = self.apply_cache.get(&key) {
            self.stats.apply_cache_hits += 1;
            return r;
        }
        self.stats.apply_calls += 1;

        if let Some(r) = self.const_step(op, a, b) {
            self.apply_cache.insert(key, r);
            return r;
        }

        let top = self.top_var(a, b);
        let (a_lo, a_hi) = self.branches(a, top);
        let (b_lo, b_hi) = self.branches(b, top);

        let lo = self.apply(op, a_lo, b_lo);
        let hi = self.apply(op, a_hi, b_hi);
        let r = self.mk(VarId(top), lo, hi);
        self.apply_cache.insert(key, r);
        r
    }

    fn const_step(&self, op: Op, a: Edge, b: Edge) -> Option<Edge> {
        if self.is_terminal_edge(a) && self.is_terminal_edge(b) {
            return None;
        }
        if self.is_terminal_edge(a) {
            let x = a.terminal_value();
            return Some(match op {
                Op::And if x => b,
                Op::And => Edge::FALSE,
                Op::Or if x => Edge::TRUE,
                Op::Or => b,
                Op::Xor if x => b.negate(),
                Op::Xor => b,
                Op::Implies if x => b,
                Op::Implies => Edge::TRUE,
                Op::Equiv if x => b,
                Op::Equiv => b.negate(),
            });
        }
        if self.is_terminal_edge(b) {
            let y = b.terminal_value();
            return Some(match op {
                Op::And if y => a,
                Op::And => Edge::FALSE,
                Op::Or if y => Edge::TRUE,
                Op::Or => a,
                Op::Xor if y => a.negate(),
                Op::Xor => a,
                Op::Implies if y => Edge::TRUE,
                Op::Implies => a.negate(),
                Op::Equiv if y => a,
                Op::Equiv => a.negate(),
            });
        }
        None
    }

    /// Variable id tested at the node an edge names.
    fn var_of_edge(&self, e: Edge) -> u32 {
        self.nodes[self.slot_of(e).unwrap()].var
    }

    fn top_var(&self, a: Edge, b: Edge) -> u32 {
        match (self.is_terminal_edge(a), self.is_terminal_edge(b)) {
            (true, _) => self.var_of_edge(b),
            (_, true) => self.var_of_edge(a),
            _ => self.var_of_edge(a).min(self.var_of_edge(b)),
        }
    }

    /// Shannon branches of an edge at expansion variable `top` (which is <=
    /// every decision variable reached). No nodes are created.
    fn branches(&self, e: Edge, top: u32) -> (Edge, Edge) {
        if self.is_terminal_edge(e) {
            (e, e)
        } else {
            let slot = self.slot_of(e).unwrap();
            let n = self.nodes[slot];
            if n.var == top {
                if e.comp() {
                    (n.low.negate(), n.high.negate())
                } else {
                    (n.low, n.high)
                }
            } else {
                debug_assert!(n.var > top);
                (e, e)
            }
        }
    }

    /// Restrict (cofactor): assign `var` to `value`.
    pub fn restrict(&mut self, var: &str, value: bool, f: &NodeRef) -> Result<NodeRef> {
        let v = self.var_id(var)?;
        let e = self.resolve(f)?;
        let mut memo: HashMap<Edge, Edge> = HashMap::new();
        let r = self.restrict_edge(v.0, value, e, &mut memo);
        self.stats.restrict_calls += 1;
        Ok(self.wrap(r))
    }

    fn restrict_edge(
        &mut self,
        v: u32,
        value: bool,
        e: Edge,
        memo: &mut HashMap<Edge, Edge>,
    ) -> Edge {
        if let Some(&r) = memo.get(&e) {
            return r;
        }
        if self.is_terminal_edge(e) {
            return e;
        }
        let slot = self.slot_of(e).unwrap();
        let node = self.nodes[slot];
        let r = if node.var > v {
            e
        } else if node.var == v {
            let chosen = if value { node.high } else { node.low };
            if e.comp() {
                chosen.negate()
            } else {
                chosen
            }
        } else {
            let l = self.restrict_edge(v, value, node.low, memo);
            let h = self.restrict_edge(v, value, node.high, memo);
            let out = self.mk(VarId(node.var), l, h);
            if e.comp() {
                out.negate()
            } else {
                out
            }
        };
        memo.insert(e, r);
        r
    }

    /// Evaluate a function under a partial assignment (absent vars = false).
    pub fn evaluate(&self, f: &NodeRef, env: &BTreeMap<String, bool>) -> Result<bool> {
        let mut e = self.resolve(f)?;
        loop {
            if e.id() == TERMINAL_ID {
                return Ok(e.comp());
            }
            let slot = self.slot_of(e)?;
            let n = self.nodes[slot];
            let take_high = *env
                .get(self.var_name(VarId(n.var)).expect("declared variable"))
                .unwrap_or(&false);
            let child = if take_high { n.high } else { n.low };
            e = if e.comp() { child.negate() } else { child };
        }
    }

    /// One satisfying assignment over the support, or `None` if unsat.
    pub fn sat_witness(&self, f: &NodeRef) -> Result<Option<BTreeMap<String, bool>>> {
        let e = self.resolve(f)?;
        if e.id() == TERMINAL_ID {
            return Ok(if e.comp() {
                Some(BTreeMap::new())
            } else {
                None
            });
        }
        let mut assign: BTreeMap<u32, bool> = BTreeMap::new();
        if self.witness(e, true, &mut assign)? {
            Ok(Some(
                assign
                    .into_iter()
                    .map(|(v, val)| (self.var_names[v as usize].clone(), val))
                    .collect(),
            ))
        } else {
            Ok(None)
        }
    }

    fn witness(&self, e: Edge, target: bool, assign: &mut BTreeMap<u32, bool>) -> Result<bool> {
        if e.id() == TERMINAL_ID {
            return Ok(e.comp() == target);
        }
        let slot = self.slot_of(e)?;
        let n = self.nodes[slot];
        let node_target = target ^ e.comp();
        for branch_value in [false, true] {
            let child = if branch_value { n.high } else { n.low };
            if self.witness(child, node_target, assign)? {
                assign.insert(n.var, branch_value);
                return Ok(true);
            }
        }
        Ok(false)
    }

    // -- introspection for verification ------------------------------------

    pub(crate) fn edge_view(&self, e: Edge) -> Result<NodeView> {
        if e.id() == TERMINAL_ID {
            return Ok(NodeView::Terminal(e.comp()));
        }
        let slot = self.slot_of(e)?;
        let n = self.nodes[slot];
        Ok(NodeView::Decision {
            var: VarId(n.var),
            low: n.low,
            high: n.high,
            edge_comp: e.comp(),
        })
    }

    pub(crate) fn ref_edge(r: &NodeRef) -> Edge {
        Edge::from_raw(r.edge_raw)
    }

    /// Recursive reachability over slots.
    pub(crate) fn mark_slot(&self, slot: usize, seen: &mut [bool]) -> Result<()> {
        if seen[slot] {
            return Ok(());
        }
        seen[slot] = true;
        let n = self.nodes[slot];
        if n.var != TERMINAL_VAR {
            self.mark_slot(self.slot_of(n.low)?, seen)?;
            self.mark_slot(self.slot_of(n.high)?, seen)?;
        }
        Ok(())
    }
}

/// Structural view exposed to the verifier.
#[derive(Clone, Copy, Debug)]
pub(crate) enum NodeView {
    Terminal(bool),
    Decision {
        var: VarId,
        low: Edge,
        high: Edge,
        edge_comp: bool,
    },
}

fn cache_key(op: Op, a: Edge, b: Edge) -> (Op, Edge, Edge) {
    if op.commutative() && a > b {
        (op, b, a)
    } else {
        (op, a, b)
    }
}
