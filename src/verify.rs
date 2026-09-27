//! Equivalence evidence: structural isomorphism *and* an independent
//! exhaustive truth-table check.
//!
//! A verdict is produced only when the BDD structure and the independent
//! [`oracle`](crate::oracle) agree. When they disagree the answer is
//! [`Decision::Inconclusive`] rather than a confident lie, and when the
//! exhaustive table is larger than the configured budget the result is also
//! inconclusive. Witnesses (a concrete disagreeing assignment) are always
//! produced by the oracle, never by the BDD.

use std::collections::{BTreeMap, HashMap};

use crate::kernel::{BddManager, Edge, ErrorKind, KernelError, NodeRef, NodeView, VarId};
use crate::lang::Expr;
use crate::oracle;

/// Final categorical verdict.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Decision {
    /// Structure and oracle both confirm equivalence.
    Accepted,
    /// Both agree the functions differ; a witness is attached.
    Rejected,
    /// Evidence is insufficient or contradictory.
    Inconclusive,
}

impl Decision {
    pub fn as_str(self) -> &'static str {
        match self {
            Decision::Accepted => "accepted",
            Decision::Rejected => "rejected",
            Decision::Inconclusive => "inconclusive",
        }
    }
}

/// Everything an evidence consumer needs to audit a verdict.
#[derive(Clone, Debug)]
pub struct EquivReport {
    pub decision: Decision,
    pub equivalent: bool,
    /// Whether the two canonical ROBDDs are isomorphic under the mapping.
    pub structural_equivalent: bool,
    /// Whether the independent exhaustive oracle was run to completion.
    pub oracle_checked: bool,
    /// Number of truth-table rows the oracle examined.
    pub assignments_checked: u64,
    /// A concrete assignment (right-side variable names) where they differ.
    pub witness: Option<BTreeMap<String, bool>>,
    /// Internal decision-node counts of the two canonical ROBDDs.
    pub lhs_internal_nodes: usize,
    pub rhs_internal_nodes: usize,
    /// Human-readable justification, citing the concrete checks.
    pub reason: String,
}

/// Inputs to a one-shot equivalence query (expressions plus each side's
/// declared variable order).
pub struct EquivQuery<'a> {
    pub lhs: &'a Expr,
    pub lhs_order: &'a [String],
    pub rhs: &'a Expr,
    pub rhs_order: &'a [String],
    /// Renaming left variable name -> right variable name. Variables absent
    /// from both supports need not be listed.
    pub mapping: BTreeMap<String, String>,
    /// Refuse exhaustive enumeration above this many rows (inclusive limit
    /// on the number of rows enumerated).
    pub max_assignments: u64,
}

/// Validate the renaming, build both ROBDDs, compare structurally, and
/// cross-check with the exhaustive oracle.
pub fn check_equivalence(q: &EquivQuery<'_>) -> Result<EquivReport, KernelError> {
    let mut lhs_mgr = BddManager::new(q.lhs_order.iter().cloned())?;
    let mut rhs_mgr = BddManager::new(q.rhs_order.iter().cloned())?;
    let lhs_ref = lhs_mgr.build(q.lhs)?;
    let rhs_ref = rhs_mgr.build(q.rhs)?;

    let lhs_support: Vec<String> = q.lhs.variables();
    let rhs_support: Vec<String> = q.rhs.variables();

    // --- Validate the variable-identity mapping -------------------------
    // Every left support variable must have an image ...
    for v in &lhs_support {
        if !q.mapping.contains_key(v) {
            return Err(KernelError::new(
                ErrorKind::UnmappedVariable,
                format!("left variable {v:?} appears but has no mapping to the right side"),
            ));
        }
    }
    // ... images must be injective on the support ...
    let mut seen_images: HashMap<&String, &String> = HashMap::new();
    for (l, r) in q.mapping.iter().filter(|(l, _)| lhs_support.contains(l)) {
        if let Some(prev) = seen_images.insert(r, l) {
            return Err(KernelError::new(
                ErrorKind::NonBijectiveMapping,
                format!("{prev:?} and {l:?} are both mapped to {r:?}"),
            ));
        }
    }
    // ... and every right support variable must be the image of a left one.
    for v in &rhs_support {
        if !q.mapping.values().any(|r| r == v) {
            return Err(KernelError::new(
                ErrorKind::UnmappedVariable,
                format!("right variable {v:?} appears but is not the image of any left variable"),
            ));
        }
    }

    // --- Order preservation on the support ------------------------------
    // Images of left support variables, visited in left order, must have
    // strictly increasing right-order positions.
    let right_pos: HashMap<&str, u32> = q
        .rhs_order
        .iter()
        .enumerate()
        .map(|(i, n)| (n.as_str(), i as u32))
        .collect();
    let mut last: Option<u32> = None;
    for l in q.lhs_order.iter().filter(|n| lhs_support.contains(n)) {
        let r = &q.mapping[l];
        let pos = right_pos.get(r.as_str()).ok_or_else(|| {
            KernelError::new(
                ErrorKind::UnknownVariable,
                format!("mapping target {r:?} is not declared in the right-side order"),
            )
        })?;
        if let Some(prev) = last {
            if *pos <= prev {
                return Err(KernelError::new(
                    ErrorKind::OrderMismatch,
                    format!(
                        "renaming reverses the fixed order: {l:?} maps to {r:?} (position {pos}) after position {prev}"
                    ),
                ));
            }
        }
        last = Some(*pos);
    }

    // VarId mapping keyed on numeric left/right indices, built by names.
    let mut id_map: HashMap<VarId, VarId> = HashMap::new();
    for (l, r) in &q.mapping {
        if lhs_support.contains(l) {
            id_map.insert(lhs_mgr.var_id(l)?, rhs_mgr.var_id(r)?);
        }
    }

    // Canonical sizes are measured from the result roots, not across the
    // whole arena (which also holds nodes from intermediate construction).
    let lhs_internal_nodes = lhs_mgr.reachable_internal_count(&lhs_ref)?;
    let rhs_internal_nodes = rhs_mgr.reachable_internal_count(&rhs_ref)?;

    // --- Structural isomorphism -----------------------------------------
    let structural = structural_equivalent(&lhs_mgr, &lhs_ref, &rhs_mgr, &rhs_ref, &id_map)?;

    // --- Independent exhaustive cross-check -----------------------------
    let renamed = rename_expr(q.lhs, &q.mapping);
    let rows = 1u64
        .checked_shl(rhs_support.len() as u32)
        .unwrap_or(u64::MAX);
    let over_limit = rows > q.max_assignments;

    let mut oracle_checked = false;
    let mut oracle_equal = true;
    let mut witness: Option<BTreeMap<String, bool>> = None;
    let mut assignments_checked = 0u64;

    if !over_limit {
        oracle_checked = true;
        assignments_checked = rows.max(1);
        let vars: Vec<String> = q
            .rhs_order
            .iter()
            .filter(|n| rhs_support.contains(n))
            .cloned()
            .collect();
        if let Some(w) = oracle::differing_assignment(&renamed, q.rhs, &vars)? {
            oracle_equal = false;
            witness = Some(w);
        }
    }

    // --- Combine evidence ------------------------------------------------
    let (decision, equivalent, reason) = match (structural, oracle_checked) {
        (true, true) if oracle_equal => (
            Decision::Accepted,
            true,
            format!(
                "accepted: canonical ROBDDs isomorphic ({lhs_internal_nodes}/{rhs_internal_nodes} internal nodes) and all {assignments_checked} truth-table rows agree"
            ),
        ),
        (false, true) if !oracle_equal => (
            Decision::Rejected,
            false,
            format!(
                "rejected: ROBDDs non-isomorphic and the independent oracle found a disagreeing assignment among {assignments_checked} rows"
            ),
        ),
        (_, true) => (
            // Structure and oracle contradict: never choose one blindly.
            Decision::Inconclusive,
            structural,
            format!(
                "inconclusive: structural verdict ({structural}) and exhaustive oracle verdict ({oracle_equal}) contradict; suspected kernel defect"
            ),
        ),
        (_, false) => (
            Decision::Inconclusive,
            structural,
            format!(
                "inconclusive: truth table has {rows} rows which exceeds the limit of {}; only structural evidence is available",
                q.max_assignments
            ),
        ),
    };

    Ok(EquivReport {
        decision,
        equivalent,
        structural_equivalent: structural,
        oracle_checked,
        assignments_checked,
        witness,
        lhs_internal_nodes,
        rhs_internal_nodes,
        reason,
    })
}

/// Rename every variable in an expression per `map` (unmapped names kept).
fn rename_expr(expr: &Expr, map: &BTreeMap<String, String>) -> Expr {
    match expr {
        Expr::Const(b) => Expr::Const(*b),
        Expr::Var(name) => Expr::Var(map.get(name).cloned().unwrap_or_else(|| name.clone())),
        Expr::Not(e) => Expr::Not(Box::new(rename_expr(e, map))),
        Expr::Binary { op, lhs, rhs } => Expr::Binary {
            op: *op,
            lhs: Box::new(rename_expr(lhs, map)),
            rhs: Box::new(rename_expr(rhs, map)),
        },
    }
}

/// Complement-edge-aware structural comparison with a variable renaming.
fn structural_equivalent(
    lhs: &BddManager,
    lref: &NodeRef,
    rhs: &BddManager,
    rref: &NodeRef,
    id_map: &HashMap<VarId, VarId>,
) -> Result<bool, KernelError> {
    let a = BddManager::ref_edge(lref);
    let b = BddManager::ref_edge(rref);
    let mut memo: HashMap<(Edge, Edge), bool> = HashMap::new();
    walk(lhs, a, rhs, b, id_map, &mut memo)
}

/// Memoized paired DFS returning whether the two edge functions are the same
/// under `id_map`. Child edges carry stable logical ids, so parity is handled
/// by pairing the branch edges with their incoming parity.
fn walk(
    lhs: &BddManager,
    a: Edge,
    rhs: &BddManager,
    b: Edge,
    id_map: &HashMap<VarId, VarId>,
    memo: &mut HashMap<(Edge, Edge), bool>,
) -> Result<bool, KernelError> {
    if let Some(&r) = memo.get(&(a, b)) {
        return Ok(r);
    }
    let va = lhs.edge_view(a)?;
    let vb = rhs.edge_view(b)?;
    let r = match (va, vb) {
        (NodeView::Terminal(x), NodeView::Terminal(y)) => x == y,
        (NodeView::Terminal(_), NodeView::Decision { .. })
        | (NodeView::Decision { .. }, NodeView::Terminal(_)) => false,
        (
            NodeView::Decision {
                var: va,
                low: la,
                high: ha,
                edge_comp: ca,
            },
            NodeView::Decision {
                var: vb,
                low: lb,
                high: hb,
                edge_comp: cb,
            },
        ) => {
            if id_map.get(&va) != Some(&vb) {
                false
            } else {
                let l_ok = walk(
                    lhs,
                    edge_with_parity(la, ca),
                    rhs,
                    edge_with_parity(lb, cb),
                    id_map,
                    memo,
                )?;
                let h_ok = walk(
                    lhs,
                    edge_with_parity(ha, ca),
                    rhs,
                    edge_with_parity(hb, cb),
                    id_map,
                    memo,
                )?;
                l_ok && h_ok
            }
        }
    };
    memo.insert((a, b), r);
    Ok(r)
}

/// The edge `e` as seen through an incoming parity `parity`: stored child
/// edges always point at the node's unnegated function, so the effective
/// reference flips when the incoming edge is complemented.
fn edge_with_parity(e: Edge, parity: bool) -> Edge {
    if parity {
        e.negate()
    } else {
        e
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lang;

    fn q<'a>(
        lhs: &'a Expr,
        lo: &'a [String],
        rhs: &'a Expr,
        ro: &'a [String],
        mapping: BTreeMap<String, String>,
    ) -> EquivQuery<'a> {
        EquivQuery {
            lhs,
            lhs_order: lo,
            rhs,
            rhs_order: ro,
            mapping,
            max_assignments: 1 << 20,
        }
    }

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn identical_syntax_is_accepted_with_concrete_counts() {
        let e = lang::parse("(a || b) && c").unwrap();
        let order = s(&["a", "b", "c"]);
        let r = check_equivalence(&q(&e, &order, &e, &order, {
            let mut m = BTreeMap::new();
            m.insert("a".into(), "a".into());
            m.insert("b".into(), "b".into());
            m.insert("c".into(), "c".into());
            m
        }))
        .unwrap();
        assert_eq!(r.decision, Decision::Accepted);
        assert!(r.equivalent);
        assert!(r.structural_equivalent);
        assert!(r.oracle_checked);
        assert_eq!(r.assignments_checked, 8);
        assert_eq!(r.witness, None);
        assert_eq!(r.lhs_internal_nodes, r.rhs_internal_nodes);
    }

    #[test]
    fn different_syntax_same_function_is_accepted() {
        // a -> b  vs  !a || b  vs  !(a && !b)
        let order = s(&["a", "b"]);
        let forms = ["a -> b", "!a || b", "!(a && !b)"];
        for x in forms {
            for y in forms {
                let ex = lang::parse(x).unwrap();
                let ey = lang::parse(y).unwrap();
                let r = check_equivalence(&q(
                    &ex,
                    &order,
                    &ey,
                    &order,
                    [("a", "a"), ("b", "b")]
                        .into_iter()
                        .map(|(a, b)| (a.into(), b.into()))
                        .collect(),
                ))
                .unwrap();
                assert_eq!(r.decision, Decision::Accepted, "{x} vs {y}: {}", r.reason);
                assert_eq!(r.lhs_internal_nodes, 2, "a->b reaches nodes on b and a");
            }
        }
    }

    #[test]
    fn renaming_variables_preserves_equivalence() {
        let order_l = s(&["x", "y"]);
        let order_r = s(&["p", "q"]);
        let l = lang::parse("x && y || !x").unwrap();
        let r = lang::parse("p && q || !p").unwrap();
        let mapping = BTreeMap::from([
            ("x".to_string(), "p".to_string()),
            ("y".to_string(), "q".to_string()),
        ]);
        let rep = check_equivalence(&q(&l, &order_l, &r, &order_r, mapping)).unwrap();
        assert_eq!(rep.decision, Decision::Accepted);
        assert_eq!(rep.witness, None);
    }

    #[test]
    fn renaming_without_order_preservation_is_refused_with_category() {
        let order_l = s(&["x", "y"]);
        let order_r = s(&["q", "p"]); // images reverse positions
        let l = lang::parse("x && y").unwrap();
        let r = lang::parse("q && p").unwrap();
        let mapping = BTreeMap::from([
            ("x".to_string(), "p".to_string()),
            ("y".to_string(), "q".to_string()),
        ]);
        let err = check_equivalence(&q(&l, &order_l, &r, &order_r, mapping)).unwrap_err();
        assert_eq!(err.kind, ErrorKind::OrderMismatch);
    }

    #[test]
    fn non_bijective_mapping_is_refused() {
        let order = s(&["a", "b", "c"]);
        let l = lang::parse("a && b").unwrap();
        let r = lang::parse("c && c").unwrap();
        let mapping = BTreeMap::from([
            ("a".to_string(), "c".to_string()),
            ("b".to_string(), "c".to_string()),
        ]);
        let err = check_equivalence(&q(&l, &order, &r, &order, mapping)).unwrap_err();
        assert_eq!(err.kind, ErrorKind::NonBijectiveMapping);
    }

    #[test]
    fn unmapped_support_variable_is_refused() {
        let order = s(&["a", "b"]);
        let l = lang::parse("a && b").unwrap();
        let r = lang::parse("a").unwrap();
        let err = check_equivalence(&q(
            &l,
            &order,
            &r,
            &order,
            BTreeMap::from([("a".into(), "a".into())]),
        ))
        .unwrap_err();
        assert_eq!(err.kind, ErrorKind::UnmappedVariable);
    }

    #[test]
    fn genuinely_different_functions_are_rejected_with_a_witness() {
        let order = s(&["a", "b"]);
        let l = lang::parse("a && b").unwrap();
        let r = lang::parse("a || b").unwrap();
        let rep = check_equivalence(&q(
            &l,
            &order,
            &r,
            &order,
            [("a", "a"), ("b", "b")]
                .into_iter()
                .map(|(a, b)| (a.into(), b.into()))
                .collect(),
        ))
        .unwrap();
        assert_eq!(rep.decision, Decision::Rejected);
        assert!(!rep.equivalent);
        let w = rep
            .witness
            .expect("a disagreement witness must be returned");
        // Independently: they differ exactly when one variable is false.
        let lv = oracle::evaluate(&l, &w).unwrap();
        let rv = oracle::evaluate(&r, &w).unwrap();
        assert_ne!(lv, rv, "the witness really must distinguish them");
        // Concrete witness for this pair: a=1,b=0 (or symmetric).
        assert_ne!(w["a"], w["b"]);
    }

    #[test]
    fn over_limit_enumeration_is_inconclusive_not_wrong() {
        let order: Vec<String> = (0..20).map(|i| format!("v{i}")).collect();
        let big_or = order.join(" || ");
        let e = lang::parse(&big_or).unwrap();
        let mapping: BTreeMap<String, String> =
            order.iter().map(|v| (v.clone(), v.clone())).collect();
        let rep = check_equivalence(&EquivQuery {
            lhs: &e,
            lhs_order: &order,
            rhs: &e,
            rhs_order: &order,
            mapping,
            max_assignments: 1024,
        })
        .unwrap();
        assert_eq!(rep.decision, Decision::Inconclusive);
        assert!(!rep.oracle_checked);
        assert!(rep.reason.contains("exceeds the limit"));
    }
}
