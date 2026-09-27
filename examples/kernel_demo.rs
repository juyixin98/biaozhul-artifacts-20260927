//! Runnable demonstration of the ROBDD kernel as a library:
//!
//! ```bash
//! cargo run --example kernel_demo
//! ```
//!
//! It builds two differently-written but identical functions, confirms they
//! intern to one canonical edge, runs every connective against an exhaustive
//! check, restricts a variable, collects garbage while preserving a root, and
//! then rebuilds.

use std::collections::BTreeMap;

use bdd_backend::kernel::{BddManager, Op};
use bdd_backend::lang;
use bdd_backend::oracle;

fn main() {
    let order = ["a", "b", "c"];
    let mut mgr = BddManager::new(order).unwrap();

    // 1. Same function, two surface forms -> identical canonical reference.
    let f1 = mgr.build(&lang::parse("a -> b").unwrap()).unwrap();
    let f2 = mgr.build(&lang::parse("!a || b").unwrap()).unwrap();
    println!("a -> b  and  !a || b  intern to the same ref: {}", f1 == f2);
    println!(
        "reachable internal nodes for a -> b: {}",
        mgr.reachable_internal_count(&f1).unwrap()
    );

    // 2. Exhaustive XOR check against the independent oracle.
    let g = mgr.build(&lang::parse("(a || b) && c").unwrap()).unwrap();
    let h = mgr.build(&lang::parse("!a ^ (b && c)").unwrap()).unwrap();
    let xor = mgr.apply_op(Op::Xor, &g, &h).unwrap();
    let mut mismatches = 0;
    for env in oracle::all_assignments(&order.iter().map(|s| s.to_string()).collect::<Vec<_>>()) {
        let gv = oracle::evaluate(&lang::parse("(a || b) && c").unwrap(), &env).unwrap();
        let hv = oracle::evaluate(&lang::parse("!a ^ (b && c)").unwrap(), &env).unwrap();
        if mgr.evaluate(&xor, &env).unwrap() != (gv ^ hv) {
            mismatches += 1;
        }
    }
    println!("XOR mismatches vs exhaustive oracle over 8 rows: {mismatches}");

    // 3. Restrict c=false and verify the cofactor independently.
    let restricted = mgr.restrict("c", false, &xor).unwrap();
    let sample: BTreeMap<String, bool> =
        BTreeMap::from([("a".into(), true), ("b".into(), false), ("c".into(), false)]);
    println!(
        "xor | c=false at a=1,b=0 => {}",
        mgr.evaluate(&restricted, &sample).unwrap()
    );

    // 4. Garbage collection preserving one root, then rebuild.
    let (report, kept) = mgr.gc(&[&f1]).unwrap();
    println!(
        "gc: {} nodes collected ({} -> {}), epoch {} -> {}",
        report.collected,
        report.nodes_before,
        report.nodes_after,
        report.epoch_before,
        report.epoch_after
    );
    let f1_new = kept[0];
    assert!(
        mgr.evaluate(&f1_new, &sample).is_ok(),
        "preserved root still evaluates"
    );
    let rebuilt = mgr.build(&lang::parse("(a || b) && c").unwrap()).unwrap();
    println!(
        "rebuilt function after gc, reachable nodes: {}",
        mgr.reachable_internal_count(&rebuilt).unwrap()
    );

    // 5. The old (pre-gc) reference is provably stale.
    let stale = mgr.not(&g);
    println!("using a pre-gc reference is rejected: {}", stale.is_err());
}
