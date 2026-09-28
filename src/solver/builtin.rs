//! Built-in dependency-free DPLL solver with unit propagation.
//!
//! This is a deliberately small, *self-contained* backend so the service runs with
//! zero external binaries. It is complete for propositional CNF but not optimized
//! for industrial scale; attach [`super::external::ExternalCliSolver`] (or any other
//! [`super::Solver`] impl) for a high-performance backend.
//!
//! The model returned for SAT is verified defensively before being reported.

use super::{CancelToken, SStatus, SolveCtx, SolveResult, Solver};
use crate::language::{Cnf, Literal, Model};

/// Default cap on internal branching steps for one query; exceeding it yields
/// [`SStatus::Unknown`] ("search limit"), never UNSAT.
pub const DEFAULT_STEP_LIMIT: u64 = 2_000_000;

pub struct DpllSolver {
    step_limit: u64,
}

impl Default for DpllSolver {
    fn default() -> Self {
        Self { step_limit: DEFAULT_STEP_LIMIT }
    }
}

impl DpllSolver {
    #[must_use]
    pub fn new(step_limit: u64) -> Self {
        Self { step_limit }
    }
}

impl Solver for DpllSolver {
    fn name(&self) -> &str {
        "builtin-dpll"
    }

    fn solve(&self, cnf: &Cnf, ctx: &SolveCtx) -> SolveResult {
        // The decision itself is budgeted; an exhausted budget is "cannot decide".
        if !ctx.budget.tick() {
            return SolveResult::unknown("solver call budget exhausted before decision");
        }
        if ctx.cancel.is_cancelled() {
            return SolveResult::unknown("cancelled before decision");
        }

        // `None` = unassigned, Some(b) = value of the variable.
        let mut assign: Vec<Option<bool>> = vec![None; cnf.nvars + 1];

        match dpll(cnf, &mut assign, self.step_limit, &ctx.cancel, &mut 0) {
            SStatus::Sat => {
                // Complete the assignment for unassigned variables (arbitrarily true)
                // so the model covers the full universe, then verify it.
                for slot in assign.iter_mut() {
                    if slot.is_none() {
                        *slot = Some(true);
                    }
                }
                let model = Model(assign.iter().map(|v| v.unwrap_or(true)).collect());
                debug_assert!(cnf.satisfied_by(&model));
                if cnf.satisfied_by(&model) {
                    SolveResult::sat(model)
                } else {
                    SolveResult::unknown("internal: generated model failed verification")
                }
            }
            SStatus::Unsat => SolveResult::unsat(),
            SStatus::Unknown => SolveResult::unknown("search step limit reached or cancelled"),
        }
    }
}

/// DPLL search. Returns the status; on SAT `assign` holds a (possibly partial) model.
fn dpll(
    cnf: &Cnf,
    assign: &mut [Option<bool>],
    step_limit: u64,
    cancel: &CancelToken,
    steps: &mut u64,
) -> SStatus {
    if *steps >= step_limit || cancel.is_cancelled() {
        return SStatus::Unknown;
    }

    // --- Unit propagation ----------------------------------------------
    let mut changed = true;
    while changed {
        changed = false;
        for clause in &cnf.constraints {
            let mut unassigned: Option<Literal> = None;
            let mut satisfied = false;
            let mut unassigned_count = 0usize;
            for &lit in &clause.literals {
                match assign[lit.var()] {
                    Some(v) if lit_value(v, lit) => {
                        satisfied = true;
                        break;
                    }
                    Some(_) => {}
                    None => {
                        unassigned_count += 1;
                        unassigned = Some(lit);
                    }
                }
            }
            if satisfied {
                continue;
            }
            if unassigned_count == 0 {
                return SStatus::Unsat; // empty/falsified under current assignment
            }
            if unassigned_count == 1 {
                let lit = unassigned.expect("unit literal");
                assign[lit.var()] = Some(lit.is_positive());
                changed = true;
            }
        }
    }

    // --- Branch variable selection (first unassigned) ------------------
    let branch = (1..=cnf.nvars).find(|&v| assign[v].is_none());
    let Some(var) = branch else {
        // Complete assignment and no falsified clause.
        return SStatus::Sat;
    };

    *steps += 1;
    if *steps >= step_limit {
        return SStatus::Unknown;
    }

    for value in [true, false] {
        let mut snapshot = assign.to_vec();
        snapshot[var] = Some(value);
        match dpll(cnf, &mut snapshot, step_limit, cancel, steps) {
            SStatus::Sat => {
                assign.copy_from_slice(&snapshot);
                return SStatus::Sat;
            }
            SStatus::Unknown => return SStatus::Unknown,
            SStatus::Unsat => {}
        }
    }
    SStatus::Unsat
}

fn lit_value(var_value: bool, lit: Literal) -> bool {
    var_value == lit.is_positive()
}
