//! In-process DPLL solver with unit propagation.
//!
//! This is a deliberately small, fully deterministic solver — not a production solver.
//! Variable selection is "first unassigned variable in id order" and the positive
//! branch is always tried first, so every extraction is reproducible. Its purpose is
//! to make the service runnable out of the box with zero external processes; real
//! workloads should point [`super::external::ExternalSolver`] at a high-performance
//! SAT solver.

use std::sync::atomic::{AtomicBool, Ordering};

use crate::language::Formula;

use super::{SolveLimits, SolveOutcome, SatSolver};

#[derive(Debug, Clone, Default)]
pub struct DpllSolver {
    /// Variables above this id are rejected (defensive bound); 0 = unbounded.
    pub var_cap: usize,
}

impl DpllSolver {
    pub fn new() -> Self {
        Self { var_cap: 0 }
    }
}

impl SatSolver for DpllSolver {
    fn name(&self) -> &str {
        "dpll"
    }

    fn solve(
        &self,
        formula: &Formula,
        mask: &[bool],
        limits: &SolveLimits,
        cancel: Option<&AtomicBool>,
    ) -> SolveOutcome {
        let max_var = super::active_max_var(formula, mask) as usize;
        if self.var_cap != 0 && max_var > self.var_cap {
            return SolveOutcome::unknown(
                format!("variable count {max_var} exceeds solver cap {}", self.var_cap),
                0,
            );
        }
        // value[v] = truth of variable v; index 0 unused.
        let mut value: Vec<Option<bool>> = vec![None; max_var + 1];
        let mut decisions = 0u64;

        match search(
            formula,
            mask,
            &mut value,
            limits.max_decisions,
            cancel,
            &mut decisions,
        ) {
            Branch::Sat => {
                // Fill unassigned variables with false; anything makes a satisfying
                // assignment once every clause is satisfied, but emit a total model.
                for v in value.iter_mut() {
                    if v.is_none() {
                        *v = Some(false);
                    }
                }
                let model = value
                    .iter()
                    .enumerate()
                    .skip(1)
                    .filter_map(|(v, x)| x.map(|b| (v as i64, b)))
                    .collect();
                SolveOutcome::sat(model, decisions)
            }
            Branch::Unsat => SolveOutcome::unsat(decisions),
            Branch::Limit(reason) => SolveOutcome::unknown(reason, decisions),
        }
    }
}

enum Branch {
    Sat,
    Unsat,
    Limit(String),
}

fn check_guard(
    limits: Option<u64>,
    decisions: u64,
    cancel: Option<&AtomicBool>,
) -> Option<Branch> {
    if let Some(budget) = limits {
        if decisions >= budget {
            return Some(Branch::Limit(format!(
                "decision budget exhausted after {decisions} decisions"
            )));
        }
    }
    if let Some(flag) = cancel {
        if flag.load(Ordering::Relaxed) {
            return Some(Branch::Limit("cancellation requested".to_string()));
        }
    }
    None
}

fn search(
    formula: &Formula,
    mask: &[bool],
    value: &mut [Option<bool>],
    limits: Option<u64>,
    cancel: Option<&AtomicBool>,
    decisions: &mut u64,
) -> Branch {
    // --- Unit propagation with fixpoint ---
    loop {
        let mut unit: Option<(usize, bool)> = None;
        let mut conflict = false;
        for (i, on) in mask.iter().enumerate() {
            if !on {
                continue;
            }
            let clause = &formula.clauses[i];
            // First unassigned literal seen; once a second appears the clause is open.
            let mut first_open: Option<(usize, bool)> = None;
            let mut satisfied = false;
            for lit in &clause.literals {
                let v = lit.var.0 as usize;
                match value[v] {
                    Some(b) if b == !lit.negated => {
                        satisfied = true;
                        break;
                    }
                    Some(_) => {}
                    None => {
                        if first_open.is_some() {
                            first_open = Some((usize::MAX, false));
                        } else {
                            first_open = Some((v, lit.negated));
                        }
                    }
                }
            }
            if satisfied {
                continue;
            }
            match first_open {
                None => {
                    // Every literal is false (or the clause is empty): conflict.
                    conflict = true;
                    break;
                }
                Some((v, negated)) if v != usize::MAX => {
                    unit = Some((v, !negated));
                    break;
                }
                Some(_) => {}
            }
        }
        if conflict {
            return Branch::Unsat;
        }
        match unit {
            Some((v, b)) => value[v] = Some(b),
            None => break,
        }
    }

    if let Some(stop) = check_guard(limits, *decisions, cancel) {
        return stop;
    }

    // --- Select first unassigned variable ---
    let pick = value.iter().enumerate().skip(1).find(|(_, v)| v.is_none());
    match pick {
        None => Branch::Sat,
        Some((v, _)) => {
            *decisions += 1;
            for branch in [true, false] {
                value[v] = Some(branch);
                match search(formula, mask, value, limits, cancel, decisions) {
                    Branch::Sat => return Branch::Sat,
                    Branch::Unsat => {}
                    Branch::Limit(r) => {
                        value[v] = None;
                        return Branch::Limit(r);
                    }
                }
            }
            value[v] = None;
            Branch::Unsat
        }
    }
}

#[cfg(test)]
mod tests {
    use crate::language::parse_dimacs;
    use crate::solver::SolveStatus;

    use super::*;
    fn status_of(cnf: &str, budget: Option<u64>) -> SolveStatus {
        let f = parse_dimacs(cnf).unwrap();
        let mask = vec![true; f.clauses.len()];
        DpllSolver::new()
            .solve(&f, &mask, &SolveLimits { max_decisions: budget, timeout_ms: None }, None)
            .status
    }

    #[test]
    fn known_sat_and_unsat() {
        assert_eq!(status_of("p cnf 1 1\n1 0\n", None), SolveStatus::Sat);
        assert_eq!(
            status_of("p cnf 1 2\n1 0\n-1 0\n", None),
            SolveStatus::Unsat
        );
        // pigeonhole 3 pigeons / 2 holes needs real branching and is unsat.
        let ph = "\
p cnf 6 9
1 2 0
3 4 0
5 6 0
-1 -3 0
-1 -5 0
-3 -5 0
-2 -4 0
-2 -6 0
-4 -6 0
";
        assert_eq!(status_of(ph, None), SolveStatus::Unsat);
    }

    #[test]
    fn budget_zero_yields_unknown_not_unsat() {
        let ph = "\
p cnf 6 9
1 2 0
3 4 0
5 6 0
-1 -3 0
-1 -5 0
-3 -5 0
-2 -4 0
-2 -6 0
-4 -6 0
";
        // A genuinely unsat formula reported Unknown when the budget forbids branching:
        // this pins down the UNKNOWN != UNSAT contract at the solver boundary.
        assert_eq!(status_of(ph, Some(0)), SolveStatus::Unknown);
    }
}
