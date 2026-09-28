//! Independent SAT oracle used ONLY by tests.
//!
//! Third implementation, deliberately different in representation and style from both
//! in-crate solvers: clauses are `Vec<Vec<i64>>` (DIMACS ints), assignments are a
//! `Vec<i8>` indexed directly by variable, and search is a plain recursive function
//! that assigns then *evaluates every clause from scratch* — no unit propagation,
//! no masks, no shared types with the crate under test.
//!
//! Everything the service claims is re-derived from these primitives:
//! - [`is_unsat`] / [`is_sat`] for independent SAT/UNSAT verdicts,
//! - [`assert_reference_mus`] for the subset-minimality definition,
//! - [`model_satisfies`] to validate a witness model the service hands back.

/// Truth values: 0 unassigned, 1 true, -1 false. Index 0 unused.
fn evaluate_clause(clause: &[i64], assign: &[i8]) -> Option<bool> {
    let mut any_open = false;
    for &lit in clause {
        let v = lit.unsigned_abs() as usize;
        let a = assign[v];
        if a == 0 {
            any_open = true;
        } else {
            let lit_true = if lit > 0 { a == 1 } else { a == -1 };
            if lit_true {
                return Some(true);
            }
        }
    }
    if any_open {
        None
    } else {
        Some(false)
    }
}

fn all_falsified(clauses: &[Vec<i64>], assign: &[i8]) -> bool {
    clauses
        .iter()
        .any(|c| evaluate_clause(c, assign) == Some(false))
}

fn backtrack(clauses: &[Vec<i64>], assign: &mut Vec<i8>, next_var: usize) -> bool {
    if all_falsified(clauses, assign) {
        return false;
    }
    let nv = assign.len();
    let mut v = next_var;
    while v < nv && assign[v] != 0 {
        v += 1;
    }
    if v == nv {
        return !all_falsified(clauses, assign);
    }
    for val in [1i8, -1] {
        assign[v] = val;
        if backtrack(clauses, assign, v + 1) {
            return true;
        }
    }
    assign[v] = 0;
    false
}

fn max_var(clauses: &[Vec<i64>]) -> usize {
    clauses
        .iter()
        .flatten()
        .map(|l| l.unsigned_abs() as usize)
        .max()
        .unwrap_or(0)
}

/// Returns Some(model) when satisfiable, None when unsatisfiable.
pub fn solve(clauses: &[Vec<i64>]) -> Option<Vec<i8>> {
    let n = max_var(clauses);
    let mut assign = vec![0i8; n + 1];
    if backtrack(clauses, &mut assign, 1) {
        Some(assign)
    } else {
        None
    }
}

pub fn is_sat(clauses: &[Vec<i64>]) -> bool {
    solve(clauses).is_some()
}

pub fn is_unsat(clauses: &[Vec<i64>]) -> bool {
    solve(clauses).is_none()
}

/// Independently evaluate a witness model (map var -> bool, as the service emits).
/// Returns false if the model leaves an appearing variable unassigned or falsifies
/// any clause.
pub fn model_satisfies(clauses: &[Vec<i64>], model: &std::collections::BTreeMap<i64, bool>) -> bool {
    for clause in clauses {
        let mut ok = false;
        for &lit in clause {
            let v = lit.abs();
            match model.get(&v) {
                Some(&b) => {
                    let lit_true = if lit > 0 { b } else { !b };
                    if lit_true {
                        ok = true;
                        break;
                    }
                }
                None => return false,
            }
        }
        if !ok {
            return false;
        }
    }
    true
}

/// The reference definition, asserted directly:
/// 1. the candidate as a whole is UNSAT, and
/// 2. removing ANY single member makes it SAT.
///
/// This is subset-minimality — it says nothing about cardinality vs. other cores.
pub fn assert_reference_mus(core: &[Vec<i64>]) {
    assert!(
        !core.is_empty(),
        "reference answer: an MUS cannot be the empty set here"
    );
    assert!(
        is_unsat(core),
        "reference answer: claimed core must be UNSAT as a whole"
    );
    for (i, _member) in core.iter().enumerate() {
        let mut reduced: Vec<Vec<i64>> = core.to_vec();
        reduced.remove(i);
        assert!(
            is_sat(&reduced),
            "reference answer: core is NOT subset-minimal; removing member #{i} still UNSAT"
        );
    }
}

/// Is `smaller` a strict subset of `larger` (as id sets)?
pub fn strict_subset(smaller: &[&str], larger: &[&str]) -> bool {
    let big: std::collections::HashSet<&str> = larger.iter().copied().collect();
    let small: std::collections::HashSet<&str> = smaller.iter().copied().collect();
    small.len() < big.len() && small.iter().all(|s| big.contains(s))
}
