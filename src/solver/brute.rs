//! Truth-table SAT solver for independent evidence.
//!
//! This enumerates all 2^n assignments in Gray-free binary order and checks every
//! clause directly. It intentionally shares no propagation/search code with
//! [`super::dpll`]: the whole point of a witness verifier is that its answer is
//! produced independently of the implementation under test.
//!
//! It is bounded by [`BruteForceSolver::max_vars`] (default 22). Above the bound it
//! returns `Unknown` rather than guessing, and the evidence layer then reports the
//! minimality certificate as inconclusive instead of accepting an unverified core.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};

use crate::language::Formula;

use super::{active_max_var, SolveLimits, SolveOutcome, SatSolver};

#[derive(Debug, Clone)]
pub struct BruteForceSolver {
    pub max_vars: usize,
}

impl Default for BruteForceSolver {
    fn default() -> Self {
        Self { max_vars: 22 }
    }
}

impl BruteForceSolver {
    pub fn new(max_vars: usize) -> Self {
        Self { max_vars }
    }

    fn satisfies(
        formula: &Formula,
        mask: &[bool],
        assignment: u64,
    ) -> bool {
        for (i, on) in mask.iter().enumerate() {
            if !on {
                continue;
            }
            let clause = &formula.clauses[i];
            if clause.literals.is_empty() {
                return false;
            }
            let mut ok = false;
            for lit in &clause.literals {
                let bit = (assignment >> (lit.var.0 as usize - 1)) & 1 == 1;
                let val = bit != lit.negated;
                if val {
                    ok = true;
                    break;
                }
            }
            if !ok {
                return false;
            }
        }
        true
    }
}

impl SatSolver for BruteForceSolver {
    fn name(&self) -> &str {
        "brute"
    }

    fn solve(
        &self,
        formula: &Formula,
        mask: &[bool],
        _limits: &SolveLimits,
        cancel: Option<&AtomicBool>,
    ) -> SolveOutcome {
        let max_var = active_max_var(formula, mask) as usize;
        if max_var > self.max_vars {
            return SolveOutcome::unknown(
                format!(
                    "truth-table verifier supports at most {} variables, subset has {max_var}",
                    self.max_vars
                ),
                0,
            );
        }
        let total: u64 = if max_var == 0 {
            1
        } else {
            1u64.checked_shl(max_var as u32).unwrap_or(u64::MAX)
        };
        let mut checks = 0u64;
        for assignment in 0..total {
            if checks.is_multiple_of(4096) {
                if let Some(flag) = cancel {
                    if flag.load(Ordering::Relaxed) {
                        return SolveOutcome::unknown("cancellation requested", checks);
                    }
                }
            }
            checks += 1;
            if Self::satisfies(formula, mask, assignment) {
                let mut model: BTreeMap<i64, bool> = BTreeMap::new();
                for v in 1..=max_var {
                    model.insert(v as i64, (assignment >> (v - 1)) & 1 == 1);
                }
                return SolveOutcome::sat(model, checks);
            }
        }
        SolveOutcome::unsat(checks)
    }
}

#[cfg(test)]
mod tests {
    use crate::language::parse_dimacs;
    use crate::solver::SolveStatus;

    use super::*;

    fn f(cnf: &str) -> Formula {
        parse_dimacs(cnf).unwrap()
    }

    #[test]
    fn independent_sat_unsat() {
        let s = BruteForceSolver::default();
        let sat = f("p cnf 2 2\n1 0\n2 0\n");
        let m = vec![true; sat.clauses.len()];
        assert_eq!(
            s.solve(&sat, &m, &SolveLimits::default(), None).status,
            SolveStatus::Sat
        );

        let unsat = f("p cnf 1 2\n1 0\n-1 0\n");
        let m = vec![true; unsat.clauses.len()];
        assert_eq!(
            s.solve(&unsat, &m, &SolveLimits::default(), None).status,
            SolveStatus::Unsat
        );
    }

    #[test]
    fn too_many_variables_is_unknown() {
        let s = BruteForceSolver::new(3);
        let big = f("p cnf 4 1\n1 2 3 4 0\n");
        let m = vec![true];
        assert_eq!(
            s.solve(&big, &m, &SolveLimits::default(), None).status,
            SolveStatus::Unknown
        );
    }
}
