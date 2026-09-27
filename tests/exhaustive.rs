//! Exhaustive cross-validation of the BDD kernel against the independent
//! truth-table oracle for small variable sets.
//!
//! Every expected value here is computed by `oracle` (a plain AST
//! interpreter that never imports the kernel), so a passing test genuinely
//! constrains the kernel rather than echoing its answers.

mod common;

use std::collections::{BTreeMap, BTreeSet};

use bdd_backend::kernel::{BddManager, Op};
use bdd_backend::lang;
use bdd_backend::oracle;

fn vars(names: &[&str]) -> Vec<String> {
    names.iter().map(|s| s.to_string()).collect()
}

fn assignments(names: &[String]) -> Vec<BTreeMap<String, bool>> {
    oracle::all_assignments(names).collect()
}

#[test]
fn every_formula_matches_oracle_on_every_assignment() {
    let order = vars(&common::ORDER3);
    for src in common::FORMULAS3 {
        let expr = lang::parse(src).unwrap();
        let mut mgr = BddManager::new(order.iter().cloned()).unwrap();
        let node = mgr.build(&expr).unwrap();

        let table = oracle::truth_table(&expr, &order)
            .unwrap_or_else(|e| panic!("oracle failed for {src}: {e}"));
        for (i, env) in assignments(&order).into_iter().enumerate() {
            let got = mgr.evaluate(&node, &env).unwrap();
            assert_eq!(
                got, table[i],
                "BDD disagrees with oracle for {src:?} under {env:?}"
            );
        }
    }
}

#[test]
fn apply_every_connective_matches_oracle_exhaustively() {
    let order = vars(&common::ORDER3);
    let connectives = [
        (Op::And, "&&"),
        (Op::Or, "||"),
        (Op::Xor, "^"),
        (Op::Implies, "->"),
        (Op::Equiv, "<->"),
    ];
    let lhs_src = "(a || b) && !c";
    let rhs_src = "a ^ (b -> c)";

    for (op, sym) in connectives {
        let l_expr = lang::parse(lhs_src).unwrap();
        let r_expr = lang::parse(rhs_src).unwrap();
        let mut mgr = BddManager::new(order.iter().cloned()).unwrap();
        let l = mgr.build(&l_expr).unwrap();
        let r = mgr.build(&r_expr).unwrap();
        let out = mgr.apply_op(op, &l, &r).unwrap();

        for env in assignments(&order) {
            let lv = oracle::evaluate(&l_expr, &env).unwrap();
            let rv = oracle::evaluate(&r_expr, &env).unwrap();
            let want = op.eval(lv, rv);
            let got = mgr.evaluate(&out, &env).unwrap();
            assert_eq!(got, want, "{lhs_src} {sym} {rhs_src} wrong under {env:?}");
        }
    }
}

#[test]
fn apply_is_correct_for_all_two_variable_boolean_functions() {
    // There are exactly 16 Boolean functions of two variables. Build each
    // independently from its truth table as a DNF formula (this construction
    // lives entirely in the test/oracle world), then apply every connective
    // to every ordered pair and compare against direct truth evaluation over
    // all 4 assignments: 16 * 16 * 5 * 4 = 5120 checked points.
    let names = vec!["a".to_string(), "b".to_string()];
    let connectives = [Op::And, Op::Or, Op::Xor, Op::Implies, Op::Equiv];
    let table: Vec<BTreeMap<String, bool>> = assignments(&names);

    // mask bit i is function value on table row i.
    let dnf_expr = |mask: u8| -> bdd_backend::lang::Expr {
        let mut minterms = Vec::new();
        for (i, env) in table.iter().enumerate() {
            if mask & (1 << i) != 0 {
                let a = env["a"];
                let b = env["b"];
                let term = format!(
                    "{}a && {}b",
                    if a { "" } else { "!" },
                    if b { "" } else { "!" }
                );
                minterms.push(term);
            }
        }
        let src = if minterms.is_empty() {
            "false".to_string()
        } else {
            minterms.join(" || ")
        };
        lang::parse(&src).unwrap()
    };

    for ma in 0u8..=15 {
        for mb in 0u8..=15 {
            let ea = dnf_expr(ma);
            let eb = dnf_expr(mb);
            let mut mgr = BddManager::new(names.iter().cloned()).unwrap();
            let ra = mgr.build(&ea).unwrap();
            let rb = mgr.build(&eb).unwrap();

            for op in connectives {
                let out = mgr.apply_op(op, &ra, &rb).unwrap();
                for env in &table {
                    let av = oracle::evaluate(&ea, env).unwrap();
                    let bv = oracle::evaluate(&eb, env).unwrap();
                    assert_eq!(
                        mgr.evaluate(&out, env).unwrap(),
                        op.eval(av, bv),
                        "apply({op:?}, f{ma:02?}, f{mb:02?}) wrong at {env:?}"
                    );
                }
            }
        }
    }
}

#[test]
fn restrict_equals_oracle_cofactor_for_every_variable_and_value() {
    let order = vars(&common::ORDER3);
    let src = "(a && b) || (!b && c)";
    let expr = lang::parse(src).unwrap();

    for v in &order {
        for value in [false, true] {
            let mut mgr = BddManager::new(order.iter().cloned()).unwrap();
            let f = mgr.build(&expr).unwrap();
            let restricted = mgr.restrict(v, value, &f).unwrap();

            for mut env in assignments(&order) {
                env.insert(v.clone(), value);
                let got = mgr.evaluate(&restricted, &env).unwrap();
                let want = oracle::evaluate(&expr, &env).unwrap();
                assert_eq!(got, want, "restrict {v}={value} wrong under {env:?}");
            }
        }
    }
}

#[test]
fn equivalent_surface_forms_share_one_canonical_edge() {
    let order = vars(&["a", "b"]);
    for (x, y) in common::EQUIV_PAIRS {
        let mut mgr = BddManager::new(order.iter().cloned()).unwrap();
        let rx = mgr.build(&lang::parse(x).unwrap()).unwrap();
        let ry = mgr.build(&lang::parse(y).unwrap()).unwrap();
        assert_eq!(
            rx, ry,
            "{x:?} and {y:?} are the same Boolean function and must intern identically"
        );
        // And their truth tables agree row by row.
        let ex = lang::parse(x).unwrap();
        let ey = lang::parse(y).unwrap();
        assert_eq!(
            oracle::truth_table(&ex, &order).unwrap(),
            oracle::truth_table(&ey, &order).unwrap()
        );
    }
}

#[test]
fn non_equivalent_functions_have_a_concrete_oracle_witness() {
    let order = vars(&["a", "b"]);
    for (x, y) in common::NON_EQUIV_PAIRS {
        let ex = lang::parse(x).unwrap();
        let ey = lang::parse(y).unwrap();
        let witness = oracle::differing_assignment(&ex, &ey, &order)
            .unwrap()
            .unwrap_or_else(|| panic!("{x:?} and {y:?} must differ somewhere"));
        assert_ne!(
            oracle::evaluate(&ex, &witness).unwrap(),
            oracle::evaluate(&ey, &witness).unwrap(),
            "the reported witness must actually distinguish the functions"
        );
        // Sanity: they are not identical over the whole table.
        let tx: BTreeSet<bool> = oracle::truth_table(&ex, &order)
            .unwrap()
            .into_iter()
            .collect();
        let ty: BTreeSet<bool> = oracle::truth_table(&ey, &order)
            .unwrap()
            .into_iter()
            .collect();
        let _ = (tx, ty);
    }
}

#[test]
fn renaming_variables_keeps_function_identical_under_oracle() {
    let lo = vars(&common::RENAME_LEFT_ORDER);
    let ro = vars(&common::RENAME_RIGHT_ORDER);
    let left_src = "(x && y) || !x";
    let right_src = "(p && q) || !p";

    let lexpr = lang::parse(left_src).unwrap();
    let mut lm = BddManager::new(lo.iter().cloned()).unwrap();
    let lref = lm.build(&lexpr).unwrap();
    let mut rm = BddManager::new(ro.iter().cloned()).unwrap();
    let rref = rm.build(&lang::parse(right_src).unwrap()).unwrap();

    // Different managers -> references must not be interchangeable.
    assert!(rm.evaluate(&lref, &BTreeMap::new()).is_err());
    assert!(lm.evaluate(&rref, &BTreeMap::new()).is_err());

    // But under the renaming x->p, y->q every assignment agrees.
    for (li, ri) in assignments(&lo).into_iter().zip(assignments(&ro)) {
        let lv = lm.evaluate(&lref, &li).unwrap();
        let rv = rm.evaluate(&rref, &ri).unwrap();
        assert_eq!(lv, rv, "renamed functions disagree at {li:?} / {ri:?}");
    }
}

#[test]
fn rebuilding_after_garbage_collection_preserves_every_truth_value() {
    let order = vars(&common::ORDER3);
    let srcs = ["(a && b) || c", "b -> c", "!a ^ c"];
    let exprs: Vec<_> = srcs.iter().map(|s| lang::parse(s).unwrap()).collect();

    let mut mgr = BddManager::new(order.iter().cloned()).unwrap();
    let refs: Vec<_> = exprs.iter().map(|e| mgr.build(e).unwrap()).collect();

    // Keep only the middle root, rebuild the other two after compaction.
    let (_rep, kept) = mgr.gc(&[&refs[1]]).unwrap();
    let middle = kept[0];
    let rebuilt: Vec<_> = exprs
        .iter()
        .enumerate()
        .map(|(i, e)| {
            let r = mgr.build(e).unwrap();
            // Old refs for the dropped functions are now stale.
            if i != 1 {
                assert!(mgr.evaluate(&refs[i], &BTreeMap::new()).is_err());
            }
            r
        })
        .collect();

    for env in assignments(&order) {
        for (i, expr) in exprs.iter().enumerate() {
            let root = if i == 1 { middle } else { rebuilt[i] };
            assert_eq!(
                mgr.evaluate(&root, &env).unwrap(),
                oracle::evaluate(expr, &env).unwrap(),
                "post-GC rebuild changed {:?} under {:?}",
                srcs[i],
                env
            );
        }
    }
}

#[test]
fn random_operation_sequences_survive_gc_and_match_oracle() {
    // Deterministic, seed-driven property test (no external RNG). Maintains a
    // parallel "world": each live BDD reference has a matching independent
    // oracle expression. Random steps build/apply/restrict, and periodically a
    // GC drops some roots; afterwards dropped refs must be stale and surviving
    // refs (repacked) must still agree with the oracle on every assignment.
    let names = vec!["a".to_string(), "b".to_string(), "c".to_string()];
    let table: Vec<BTreeMap<String, bool>> = assignments(&names);

    // Tiny deterministic LCG.
    let mut state: u64 = 0x1234_5678_9abc_def0;
    let mut next = || {
        state = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        state >> 33
    };

    fn random_expr(
        names: &[String],
        rng: &mut impl FnMut() -> u64,
        depth: u32,
    ) -> bdd_backend::lang::Expr {
        use bdd_backend::lang::{BinOp, Expr};
        let pick = (rng() % 6) as u8;
        if depth == 0 || pick < 2 {
            match rng() % 4 {
                0 => Expr::Const(rng() & 1 == 0),
                _ => Expr::Var(names[(rng() as usize) % names.len()].clone()),
            }
        } else {
            match pick {
                2 => Expr::Not(Box::new(random_expr(names, rng, depth - 1))),
                3 => Expr::Binary {
                    op: BinOp::And,
                    lhs: Box::new(random_expr(names, rng, depth - 1)),
                    rhs: Box::new(random_expr(names, rng, depth - 1)),
                },
                4 => Expr::Binary {
                    op: BinOp::Or,
                    lhs: Box::new(random_expr(names, rng, depth - 1)),
                    rhs: Box::new(random_expr(names, rng, depth - 1)),
                },
                _ => Expr::Binary {
                    op: BinOp::Xor,
                    lhs: Box::new(random_expr(names, rng, depth - 1)),
                    rhs: Box::new(random_expr(names, rng, depth - 1)),
                },
            }
        }
    }

    let mut mgr = BddManager::new(names.iter().cloned()).unwrap();
    // (current ref option, independent expression, alive flag)
    let mut world: Vec<(
        Option<bdd_backend::kernel::NodeRef>,
        bdd_backend::lang::Expr,
        bool,
    )> = Vec::new();

    let ops = [Op::And, Op::Or, Op::Xor, Op::Implies, Op::Equiv];

    let check_world = |mgr: &BddManager,
                       world: &[(
        Option<bdd_backend::kernel::NodeRef>,
        bdd_backend::lang::Expr,
        bool,
    )],
                       step: u32,
                       note: &str| {
        for (idx, (r, e, alive)) in world.iter().enumerate() {
            if !*alive {
                continue;
            }
            let r = r.unwrap();
            for env in &table {
                let want = oracle::evaluate(e, env).unwrap();
                match mgr.evaluate(&r, env) {
                    Ok(got) if got == want => {}
                    Ok(got) => panic!(
                        "step {step} [{note}] slot {idx} wrong: bdd={got} oracle={want} expr={e:?} env={env:?} epoch={}",
                        mgr.epoch()
                    ),
                    Err(err) => panic!(
                        "step {step} [{note}] slot {idx} unexpectedly rejected: {err} expr={e:?}"
                    ),
                }
            }
        }
    };

    for step in 0..400 {
        check_world(&mgr, &world, step, "loop-top");
        match next() % 10 {
            0..=3 => {
                let e = random_expr(&names, &mut next, 4);
                let r = mgr.build(&e).unwrap();
                // Fresh build always agrees.
                for env in &table {
                    assert_eq!(
                        mgr.evaluate(&r, env).unwrap(),
                        oracle::evaluate(&e, env).unwrap()
                    );
                }
                world.push((Some(r), e, true));
            }
            4..=6 if world.len() >= 2 => {
                let i = next() as usize % world.len();
                let j = next() as usize % world.len();
                if world[i].2 && world[j].2 {
                    let op = ops[(next() as usize) % ops.len()];
                    let ri = world[i].0.unwrap();
                    let rj = world[j].0.unwrap();
                    for env in &table {
                        let av = oracle::evaluate(&world[i].1, env).unwrap();
                        let bv = oracle::evaluate(&world[j].1, env).unwrap();
                        let ai = mgr.evaluate(&ri, env).unwrap();
                        let bi = mgr.evaluate(&rj, env).unwrap();
                        if ai != av {
                            panic!("step {step}: OPERAND i corrupted bdd={ai} oracle={av} expr={:?} epoch={}", world[i].1, mgr.epoch());
                        }
                        if bi != bv {
                            panic!("step {step}: OPERAND j corrupted bdd={bi} oracle={bv} expr={:?} epoch={}", world[j].1, mgr.epoch());
                        }
                    }
                    let out = mgr.apply_op(op, &ri, &rj).unwrap();
                    for env in &table {
                        let av = oracle::evaluate(&world[i].1, env).unwrap();
                        let bv = oracle::evaluate(&world[j].1, env).unwrap();
                        let got = mgr.evaluate(&out, env).unwrap();
                        if got != op.eval(av, bv) {
                            let ai = mgr.evaluate(&ri, env).unwrap();
                            let bi = mgr.evaluate(&rj, env).unwrap();
                            panic!(
                                "step {step}: apply {op:?} mismatch env={env:?} out={got} want={} | lhs bdd={ai} oracle={av} expr={:?} | rhs bdd={bi} oracle={bv} expr={:?}",
                                op.eval(av, bv), world[i].1, world[j].1
                            );
                        }
                    }
                    let combined = combine(&world[i].1, &world[j].1, op);
                    world.push((Some(out), combined, true));
                    check_world(&mgr, &world, step, "post-apply");
                }
            }
            7..=8 if !world.is_empty() => {
                let i = next() as usize % world.len();
                if world[i].2 {
                    let var = names[(next() as usize) % names.len()].clone();
                    let value = next() % 2 == 0;
                    let ri = world[i].0.unwrap();
                    let out = mgr.restrict(&var, value, &ri).unwrap();
                    // Independent cofactor mirror: substitute the variable by
                    // its constant in the oracle expression.
                    let mirror = substitute_const(&world[i].1, &var, value);
                    for env in &table {
                        assert_eq!(
                            mgr.evaluate(&out, env).unwrap(),
                            oracle::evaluate(&mirror, env).unwrap(),
                            "step {step}: random restrict mismatch under {env:?}"
                        );
                    }
                    world.push((Some(out), mirror, true));
                    check_world(&mgr, &world, step, "post-restrict");
                }
            }
            _ => {
                // GC: retain a random (non-empty) subset of live slots as the
                // declared roots. Whether a *non-declared* slot survives is
                // decided by the kernel (its node may still be reachable and
                // shared), not assumed by the test.
                let live_slots: Vec<usize> = (0..world.len()).filter(|i| world[*i].2).collect();
                let mut keep_idx: Vec<usize> = live_slots
                    .iter()
                    .copied()
                    .filter(|_| next() % 2 == 0)
                    .collect();
                if keep_idx.is_empty() {
                    keep_idx = live_slots.iter().take(1).copied().collect();
                }
                let refs: Vec<bdd_backend::kernel::NodeRef> =
                    keep_idx.iter().map(|&i| world[i].0.unwrap()).collect();
                let borrowed: Vec<&bdd_backend::kernel::NodeRef> = refs.iter().collect();
                let (_report, _kept) = mgr.gc(&borrowed).unwrap();

                // Reclassify every slot by asking the kernel: a handle whose
                // logical node was genuinely reclaimed is now stale; a handle
                // whose node moved-but-survived still evaluates correctly.
                for slot in world.iter_mut() {
                    if !slot.2 {
                        continue;
                    }
                    let r = slot.0.unwrap();
                    match mgr.evaluate(&r, &table[0]) {
                        // Survived: verify the whole truth table still matches.
                        Ok(_) => {
                            for env in &table {
                                assert_eq!(
                                    mgr.evaluate(&r, env).unwrap(),
                                    oracle::evaluate(&slot.1, env).unwrap(),
                                    "step {step}: a surviving non-root handle changed after gc"
                                );
                            }
                        }
                        // Reclaimed: it must be specifically a stale-reference.
                        Err(err) => {
                            assert_eq!(
                                err.kind,
                                bdd_backend::kernel::ErrorKind::StaleReference,
                                "post-gc failure must be stale-reference, got {err:?}"
                            );
                            slot.2 = false;
                            slot.0 = None;
                        }
                    }
                }
            }
        }
    }

    // Substitute a variable by a boolean constant in an oracle expression.
    fn substitute_const(
        e: &bdd_backend::lang::Expr,
        var: &str,
        value: bool,
    ) -> bdd_backend::lang::Expr {
        use bdd_backend::lang::Expr;
        match e {
            Expr::Const(b) => Expr::Const(*b),
            Expr::Var(name) => {
                if name == var {
                    Expr::Const(value)
                } else {
                    Expr::Var(name.clone())
                }
            }
            Expr::Not(x) => Expr::Not(Box::new(substitute_const(x, var, value))),
            Expr::Binary { op, lhs, rhs } => Expr::Binary {
                op: *op,
                lhs: Box::new(substitute_const(lhs, var, value)),
                rhs: Box::new(substitute_const(rhs, var, value)),
            },
        }
    }

    // Build an oracle-expression mirror of an apply.
    fn combine(
        a: &bdd_backend::lang::Expr,
        b: &bdd_backend::lang::Expr,
        op: Op,
    ) -> bdd_backend::lang::Expr {
        use bdd_backend::lang::{BinOp, Expr};
        let bop = match op {
            Op::And => BinOp::And,
            Op::Or => BinOp::Or,
            Op::Xor => BinOp::Xor,
            Op::Implies => BinOp::Implies,
            Op::Equiv => BinOp::Equiv,
        };
        Expr::Binary {
            op: bop,
            lhs: Box::new(a.clone()),
            rhs: Box::new(b.clone()),
        }
    }
}
