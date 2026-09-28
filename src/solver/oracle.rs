//! Independent brute-force (truth-table) solver.
//!
//! This is a deliberately *different* implementation from [`super::builtin`] DPLL:
//! it enumerates total assignments in Gray-ish binary order and evaluates clauses
//! directly. Its purpose is independent cross-checking of small instances — it has
//! no unit propagation or shared code with the extraction kernel.
//!
//! It is deliberately bounded: above `MAX_NVARS` (or a call-count step cap) it
//! returns [`SolveResult::unknown`] instead of pretending to decide.

use super::{SolveCtx, SolveResult, Solver};
use crate::language::{Cnf, Literal, Model};

/// Instance sizes above which enumeration is refused (2²² ≈ 4.2m assignments).
pub const MAX_NVARS: usize = 22;
const STEP_CAP: u64 = 8_000_000;

pub struct BruteForceSolver;

impl BruteForceSolver {
    #[must_use]
    pub fn new() -> Self {
        Self
    }
}

impl Default for BruteForceSolver {
    fn default() -> Self {
        Self
    }
}

impl Solver for BruteForceSolver {
    fn name(&self) -> &str {
        "independent-bruteforce"
    }

    fn solve(&self, cnf: &Cnf, ctx: &SolveCtx) -> SolveResult {
        if !ctx.budget.tick() {
            return SolveResult::unknown("verifier budget exhausted");
        }
        if ctx.cancel.is_cancelled() {
            return SolveResult::unknown("cancelled");
        }
        if cnf.nvars > MAX_NVARS {
            return SolveResult::unknown(format!(
                "brute-force oracle refuses nvars={} > {MAX_NVARS}",
                cnf.nvars
            ));
        }

        // Empty clause present ⇒ trivially UNSAT regardless of assignment.
        if cnf.constraints.iter().any(|c| c.literals.is_empty()) {
            return SolveResult::unsat();
        }
        if cnf.constraints.is_empty() {
            return SolveResult::sat(Model(vec![true; cnf.nvars + 1]));
        }

        let n = cnf.nvars;
        let total: u64 = if n == 0 { 1 } else { 1u64 << n };
        for (steps, mask) in (0..total).enumerate() {
            let steps = steps as u64;
            if steps & 0x3fff == 0 && (ctx.cancel.is_cancelled() || steps >= STEP_CAP) {
                return SolveResult::unknown("oracle search cap or cancel");
            }
            // `mask`: bit v-1 == value of variable v.
            if satisfies(cnf, mask, n) {
                let mut values = vec![true; n + 1];
                for (v, slot) in values.iter_mut().enumerate().skip(1) {
                    *slot = (mask >> (v - 1)) & 1 == 1;
                }
                let model = Model(values);
                debug_assert!(cnf.satisfied_by(&model));
                return SolveResult::sat(model);
            }
        }
        SolveResult::unsat()
    }
}

#[inline]
fn satisfies(cnf: &Cnf, mask: u64, _n: usize) -> bool {
    cnf.constraints.iter().all(|c| {
        c.literals
            .iter()
            .any(|Literal(signed)| {
                let v = signed.unsigned_abs() as usize;
                let bit = (mask >> (v - 1)) & 1 == 1;
                if *signed > 0 { bit } else { !bit }
            })
    })
}
