//! Kernel unit tests: fixed-order interning, reduction, apply, restrict,
//! cross-manager/stale checks, and garbage collection.
//!
//! Where a semantic claim is made (equivalence, truth value), the expected
//! values are computed independently, never by calling the code under test.

use std::collections::BTreeMap;

use crate::kernel::{BddManager, ErrorKind, Op};
use crate::lang;
use crate::oracle;

fn parse(src: &str) -> crate::lang::Expr {
    lang::parse(src).unwrap_or_else(|e| panic!("parse failed: {e}"))
}

fn eval_independent(src: &str, values: &[(&str, bool)], order: &[&str]) -> bool {
    let expr = parse(src);
    let mut map = BTreeMap::new();
    for (k, v) in values {
        map.insert((*k).to_string(), *v);
    }
    // Fill any remaining order vars with false so evaluation is total.
    for v in order {
        map.entry((*v).to_string()).or_insert(false);
    }
    oracle::evaluate(&expr, &map).expect("independent evaluation")
}

#[test]
fn rejects_duplicate_variable_order() {
    let err = BddManager::new(["a", "b", "a"])
        .err()
        .expect("duplicate order must fail");
    assert_eq!(err.kind, ErrorKind::InvalidOrder);
    assert!(err.detail.contains("duplicate variable"));
}

#[test]
fn builds_terminal_constants_without_nodes() {
    let mut m = BddManager::new(["a"]).unwrap();
    let t = m.build(&parse("true")).unwrap();
    let f = m.build(&parse("false")).unwrap();
    assert_eq!(
        m.internal_count(),
        0,
        "constants allocate no decision nodes"
    );
    assert_ne!(t.edge_raw(), f.edge_raw());
}

#[test]
fn negation_is_free_and_involutive() {
    let mut m = BddManager::new(["a", "b"]).unwrap();
    let x = m.build(&parse("a && b")).unwrap();
    let before = m.internal_count();
    let nx = m.not(&x).unwrap();
    let nnx = m.not(&nx).unwrap();
    assert_eq!(m.internal_count(), before, "negation allocates no nodes");
    assert_eq!(nnx, x, "double negation returns the identical reference");
}

#[test]
fn redundant_node_elimination_collapses_tautologies() {
    let mut m = BddManager::new(["a", "b"]).unwrap();
    // a || !a is constant true regardless of a.
    let r = m.build(&parse("a || !a")).unwrap();
    assert_eq!(
        m.reachable_internal_count(&r).unwrap(),
        0,
        "the result reaches no decision node (intermediate nodes are irrelevant)"
    );
    let w = m.sat_witness(&r).unwrap().expect("true is satisfiable");
    assert!(w.is_empty(), "constant true needs no assignments");

    let f = m.build(&parse("a && !a")).unwrap();
    assert!(m.sat_witness(&f).unwrap().is_none(), "a && !a is unsat");
}

#[test]
fn unique_table_deduplicates_isomorphic_subfunctions() {
    let mut m = BddManager::new(["a", "b", "c"]).unwrap();
    // (a&&b)||c built in two different surface orders.
    let e1 = parse("(a && b) || c");
    let e2 = parse("c || (b && a)");
    let r1 = m.build(&e1).unwrap();
    let n1 = m.reachable_internal_count(&r1).unwrap();
    let r2 = m.build(&e2).unwrap();
    let n2 = m.reachable_internal_count(&r2).unwrap();
    assert_eq!(r1, r2, "same canonical function -> identical reference");
    assert_eq!(n1, n2, "rebuild interns to the same canonical size");
    // Exact independently-derived ROBDD size for (a&&b)||c over order a<b<c:
    // nodes for c, b, a = 3.
    assert_eq!(n1, 3, "independently counted canonical node count");
}

#[test]
fn variable_order_changes_node_count_but_not_function() {
    // XOR3 over a,b,c. With complement edges the canonical reachable size is
    // 3 nodes (independent hand derivation); the arena may additionally hold
    // nodes created for intermediate subexpressions.
    let mut m1 = BddManager::new(["a", "b", "c"]).unwrap();
    let r1 = m1.build(&parse("(a ^ b) ^ c")).unwrap();
    assert_eq!(m1.reachable_internal_count(&r1).unwrap(), 3);

    let mut m2 = BddManager::new(["c", "b", "a"]).unwrap();
    let r2 = m2.build(&parse("(c ^ b) ^ a")).unwrap();
    assert_eq!(m2.reachable_internal_count(&r2).unwrap(), 3);

    // Same function under independent oracle, despite order/renaming.
    for bits in 0..8u32 {
        let a = bits & 1 != 0;
        let b = bits & 2 != 0;
        let c = bits & 4 != 0;
        let want = a ^ b ^ c;
        assert_eq!(
            oracle::evaluate(
                &parse("a ^ b ^ c"),
                &btreemap(&[("a", a), ("b", b), ("c", c)])
            )
            .unwrap(),
            want
        );
        let _ = (r1.edge_raw(), r2.edge_raw());
    }
}

fn btreemap(kv: &[(&str, bool)]) -> BTreeMap<String, bool> {
    kv.iter().map(|(k, v)| ((*k).to_string(), *v)).collect()
}

#[test]
fn apply_matches_independent_truth_table_for_all_ops() {
    let ops = [
        (Op::And, "&&"),
        (Op::Or, "||"),
        (Op::Xor, "^"),
        (Op::Implies, "->"),
        (Op::Equiv, "<->"),
    ];
    let vars = ["a", "b", "c"];
    for (op, sym) in ops {
        let mut m = BddManager::new(vars).unwrap();
        // Two pseudo-random-ish independent functions.
        let f = m.build(&parse("(a || b) && c")).unwrap();
        let g = m.build(&parse("!a ^ (b && c)")).unwrap();
        let r = m.apply_op(op, &f, &g).unwrap();

        for bits in 0..8u32 {
            let a = bits & 1 != 0;
            let b = bits & 2 != 0;
            let c = bits & 4 != 0;
            let fv = eval_independent("(a || b) && c", &[("a", a), ("b", b), ("c", c)], &vars);
            let gv = eval_independent("!a ^ (b && c)", &[("a", a), ("b", b), ("c", c)], &vars);
            let want = op.eval(fv, gv);
            let got = m
                .evaluate(&r, &btreemap(&[("a", a), ("b", b), ("c", c)]))
                .unwrap();
            assert_eq!(got, want, "op {sym:?} wrong at a={a},b={b},c={c}");
        }
    }
}

#[test]
fn restrict_matches_independent_cofactor() {
    let mut m = BddManager::new(["a", "b", "c"]).unwrap();
    let f = m.build(&parse("(a && b) || (!a && c)")).unwrap();

    // Restrict a=true -> b ; a=false -> c
    let r_true = m.restrict("a", true, &f).unwrap();
    let r_false = m.restrict("a", false, &f).unwrap();

    for b in [false, true] {
        for c in [false, true] {
            let got_b = m
                .evaluate(&r_true, &btreemap(&[("b", b), ("c", c)]))
                .unwrap();
            assert_eq!(got_b, b, "f|a=1 must equal b (b={b}, c={c})");
            let got_c = m
                .evaluate(&r_false, &btreemap(&[("b", b), ("c", c)]))
                .unwrap();
            assert_eq!(got_c, c, "f|a=0 must equal c (b={b}, c={c})");
        }
    }
}

#[test]
fn unknown_variable_is_a_typed_error() {
    let mut m = BddManager::new(["a"]).unwrap();
    let err = m.build(&parse("a && z")).unwrap_err();
    assert_eq!(err.kind, ErrorKind::UnknownVariable);
    assert!(err.detail.contains('z'));
}

#[test]
fn references_cannot_cross_managers() {
    let mut m1 = BddManager::new(["a"]).unwrap();
    let m2 = BddManager::new(["a"]).unwrap();
    let r = m1.build(&parse("a")).unwrap();
    let err = m2.not(&r).unwrap_err();
    assert_eq!(err.kind, ErrorKind::ForeignManager);
    assert!(err.detail.contains("manager"));
    assert!(err.detail.contains(&m1.id().to_string()));
}

#[test]
fn references_go_stale_after_garbage_collection() {
    let mut m = BddManager::new(["a", "b"]).unwrap();
    let keep = m.build(&parse("a")).unwrap();
    let throwaway = m.build(&parse("b")).unwrap();
    assert!(m.internal_count() >= 2);

    // Collect while preserving only `keep`.
    let (report, new_roots) = m.gc(&[&keep]).unwrap();
    assert!(report.collected >= 1, "b's node must be collected");
    assert_eq!(new_roots.len(), 1);

    // The old reference is rejected, not misinterpreted.
    let stale_err = m.not(&throwaway).unwrap_err();
    assert_eq!(stale_err.kind, ErrorKind::StaleReference);

    // The repacked root still works and denotes `a`.
    let fresh_keep = &new_roots[0];
    assert!(
        m.evaluate(fresh_keep, &btreemap(&[("a", true), ("b", false)]))
            .unwrap(),
        "a must be true when a=true"
    );
    assert!(
        !m.evaluate(fresh_keep, &btreemap(&[("a", false), ("b", true)]))
            .unwrap(),
        "a must be false when a=false"
    );
}

#[test]
fn gc_preserves_references_to_nodes_still_reachable_from_other_roots() {
    let mut m = BddManager::new(["a", "b", "c"]).unwrap();
    // `sub` is a subfunction genuinely present in `whole`'s reduced graph.
    let sub = m.build(&parse("a && b")).unwrap();
    // (c && !c) is constant false, so OR reduces the root to the identical
    // canonical a&&b edge — sub is reachable from whole (they are the same id).
    let whole = m.build(&parse("(a && b) || (c && !c)")).unwrap();
    assert_eq!(
        sub, whole,
        "OR false is a reduction to the same canonical edge"
    );

    // Declare only `whole` as a GC root.
    let (report, kept) = m.gc(&[&whole]).unwrap();
    assert!(
        report.collected >= 1,
        "the orphan c node from construction is reclaimed"
    );
    // The repacked root names the same stable logical edge.
    assert_eq!(kept[0].edge_raw(), whole.edge_raw());

    // The original (pre-GC) reference still names the same live logical node.
    for a in [false, true] {
        for b in [false, true] {
            let env = btreemap(&[("a", a), ("b", b), ("c", false)]);
            assert_eq!(m.evaluate(&sub, &env).unwrap(), a && b);
        }
    }
}

#[test]
fn unreferenced_disjoint_node_is_reclaimed_but_shared_one_is_kept() {
    let mut m = BddManager::new(["a", "b", "c"]).unwrap();
    let shared = m.build(&parse("a && b")).unwrap();
    let _whole = m.build(&parse("a && b")).unwrap(); // same canonical id
    let orphan = m.build(&parse("c")).unwrap();

    // Root only the shared function: c is disjoint and must be reclaimed.
    let (report, _) = m.gc(&[&shared]).unwrap();
    assert!(report.collected >= 1);
    // c's logical node is now stale...
    assert_eq!(m.not(&orphan).unwrap_err().kind, ErrorKind::StaleReference);
    // ...while the shared reference survives.
    assert!(m
        .evaluate(
            &shared,
            &btreemap(&[("a", true), ("b", true), ("c", false)])
        )
        .unwrap());
}

#[test]
fn gc_keeps_every_root_reachable_and_reduces_exactly_dead_nodes() {
    let mut m = BddManager::new(["a", "b", "c"]).unwrap();
    let _f1 = m.build(&parse("a && b && c")).unwrap();
    let f2 = m.build(&parse("a || c")).unwrap();
    let before = m.internal_count();

    // Keep f2 only: its reachable set is {c-node, a-node}.
    let live_before = m.reachable_count(&[&f2]).unwrap();
    let (report, kept) = m.gc(&[&f2]).unwrap();
    assert_eq!(
        report.collected,
        before + 1 - live_before,
        "exactly the unreachable internal slots go (arena includes terminal slot)"
    );
    assert_eq!(m.node_count(), live_before);
    let f2_kept = &kept[0];

    // A second pass over the already-minimal graph reclaims nothing.
    let (report2, roots2) = m.gc(&[f2_kept]).unwrap();
    assert_eq!(report2.collected, 0, "nothing dead after first sweep");
    assert_eq!(roots2.len(), 1);
}

#[test]
fn gc_then_rebuild_reproduces_canonical_structure() {
    let mut m = BddManager::new(["a", "b", "c"]).unwrap();
    let f1 = m.build(&parse("a && b")).unwrap();
    let _f2 = m.build(&parse("b || c")).unwrap();
    let (_, kept) = m.gc(&[&f1]).unwrap(); // drops f2 and the c node
    let f1 = kept[0];

    // Rebuild f2 after compaction; it must produce the same function with
    // fresh nodes and coexist with the repacked f1 again.
    let f2b = m.build(&parse("b || c")).unwrap();
    for bits in 0..8u32 {
        let a = bits & 1 != 0;
        let b = bits & 2 != 0;
        let c = bits & 4 != 0;
        let env = btreemap(&[("a", a), ("b", b), ("c", c)]);
        assert_eq!(m.evaluate(&f2b, &env).unwrap(), b || c);
        assert_eq!(m.evaluate(&f1, &env).unwrap(), a && b);
    }
}

#[test]
fn complement_edges_give_a_single_representation_per_function() {
    let mut m = BddManager::new(["a", "b"]).unwrap();
    let x = m.build(&parse("a -> b")).unwrap();
    // a -> b  ==  !(a && !b); the negated edge must be the unique encoding.
    let y = m.build(&parse("!(a && !b)")).unwrap();
    assert_eq!(x, y, "equivalent expressions intern to the same edge");
    let nx = m.build(&parse("a && !b")).unwrap();
    assert_eq!(m.not(&x).unwrap(), nx, "negation shares the exact node set");
    assert_eq!(
        m.reachable_internal_count(&x).unwrap(),
        2,
        "a->b reaches exactly the nodes on b and a"
    );
}

#[test]
fn sat_witness_respects_complemented_roots() {
    let mut m = BddManager::new(["a", "b"]).unwrap();
    let f = m.build(&parse("a && b")).unwrap();
    let nf = m.not(&f).unwrap();

    // Witness of f forces a=b=true.
    let w = m.sat_witness(&f).unwrap().unwrap();
    assert_eq!(w.get("a"), Some(&true));
    assert_eq!(w.get("b"), Some(&true));

    // Witness of ¬f exists and really falsifies f under the oracle. The
    // witness only names variables on the chosen path, so complete it.
    let mut wn = m.sat_witness(&nf).unwrap().unwrap();
    wn.entry("a".to_string()).or_insert(false);
    wn.entry("b".to_string()).or_insert(false);
    let expr = parse("a && b");
    assert!(!oracle::evaluate(&expr, &wn).unwrap());
}

#[test]
fn malformed_node_slot_is_rejected() {
    let m = BddManager::new(["a"]).unwrap();
    // Logical id 0 is never a valid node.
    let bogus_zero = crate::kernel::NodeRef {
        manager_id: m.id(),
        epoch: m.epoch(),
        edge_raw: 0,
    };
    assert_eq!(m.not(&bogus_zero).unwrap_err().kind, ErrorKind::InvalidNode);

    // A plausible id that was never allocated/reclaimed is stale.
    let bogus_huge = crate::kernel::NodeRef {
        manager_id: m.id(),
        epoch: m.epoch(),
        edge_raw: u32::MAX,
    };
    assert_eq!(
        m.not(&bogus_huge).unwrap_err().kind,
        ErrorKind::StaleReference
    );
}
