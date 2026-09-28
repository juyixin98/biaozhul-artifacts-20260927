//! 求解内核对外门面（facade）：规范化输入 → DPLL 求解 → 带证据结论。

pub mod budget;
pub mod clause;
pub mod engine;

pub use budget::{Budget, BudgetExceeded, Counters};
pub use engine::Solver;

use crate::evidence::types::{NormalizeReport, Outcome};
use crate::normalize::NormalizedCnf;

/// 一次求解的完整结果。
pub struct SolveResult {
    pub outcome: Outcome,
    pub counters: Counters,
    pub budget_exceeded: Option<BudgetExceeded>,
    pub normalize_report: NormalizeReport,
}

/// 对已规范化公式求解。
pub fn solve_normalized(cnf: &NormalizedCnf, budget: &Budget) -> SolveResult {
        let normalize_report = NormalizeReport {
            num_vars_effective: cnf.effective_vars(),
            num_clauses_after: cnf.clauses.len(),
            duplicate_literals_removed: cnf.duplicate_literals_removed,
            tautology_clauses_removed: cnf.tautology_clauses_removed,
            has_empty_clause: cnf.has_empty_clause(),
        };
        let solver = Solver::new(cnf);
        let (outcome, counters, budget_exceeded) = solver.solve(budget);
        SolveResult {
            outcome,
            counters,
            budget_exceeded,
            normalize_report,
        }
}
