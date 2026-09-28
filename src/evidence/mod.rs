//! Witness/evidence verification for extracted cores.
//!
//! A core report is only *certified* after an independent re-check:
//!
//! 1. **Unsat check** — the core as a whole must be proved UNSAT by the verifier.
//! 2. **Member minimality** — for every member `m`, the core minus `m` must be proved
//!    SAT, and the satisfying model is retained as a witness for *why* `m` is needed.
//!
//! The verifier solver is injected separately from the extraction solver on purpose:
//! a system that certifies itself is not providing independent evidence. When the
//! verifier cannot decide (Unknown — e.g. truth-table bound, timeout) the verdict is
//! `Inconclusive`, and the caller can see exactly which checks failed to decide.

use std::collections::BTreeMap;
use std::sync::atomic::AtomicBool;

use crate::language::{ClauseId, Formula};
use crate::solver::{SatSolver, SolveLimits, SolveStatus};

use serde::{Deserialize, Serialize};

/// Outcome of independently checking one claimed core.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    /// Core is UNSAT and every single-member deletion is SAT: subset-minimal.
    Certified,
    /// The core as a whole was not proved UNSAT — extraction output rejected.
    CoreNotUnsat,
    /// Removing some member still leaves an UNSAT formula — not minimal, rejected.
    NotMinimal,
    /// Some needed solver call returned Unknown: no claim either way.
    Inconclusive,
}

/// Evidence for one member clause: why does removing it make the formula satisfiable?
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemberEvidence {
    pub member: ClauseId,
    /// Verifier verdict for `core - member`.
    pub status: SolveStatus,
    /// Witness model when status == sat. Kept under redaction control upstream.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub witness_model: Option<BTreeMap<i64, bool>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VerificationReport {
    pub verdict: Verdict,
    pub verifier_solver: String,
    /// Satisfying/unsat status of the core taken as a whole.
    pub whole_status: SolveStatus,
    /// One entry per member, in core order.
    pub member_evidence: Vec<MemberEvidence>,
    /// Human-readable explanation of the first reason the report is not Certified.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    pub verifier_calls: usize,
}

/// Independently certify that `core_positions` (positions into `formula.clauses`) is a
/// subset-minimal UNSAT core.
///
/// Returns a fully populated report; never panics on solver failures — inability to
/// decide degrades to [`Verdict::Inconclusive`].
pub fn certify(
    formula: &Formula,
    core_positions: &[usize],
    verifier: &dyn SatSolver,
    limits: &SolveLimits,
    cancel: Option<&AtomicBool>,
) -> VerificationReport {
    let name = verifier.name().to_string();
    let mut calls = 0usize;
    let mut mask = vec![false; formula.clauses.len()];
    for &p in core_positions {
        mask[p] = true;
    }

    let whole = verifier.solve(formula, &mask, limits, cancel);
    calls += 1;
    if whole.status == SolveStatus::Sat {
        return VerificationReport {
            verdict: Verdict::CoreNotUnsat,
            verifier_solver: name,
            whole_status: SolveStatus::Sat,
            member_evidence: vec![],
            note: Some("verifier found a satisfying model of the claimed core".to_string()),
            verifier_calls: calls,
        };
    }

    let mut member_evidence = Vec::with_capacity(core_positions.len());
    let mut inconclusive = whole.status == SolveStatus::Unknown;

    for &p in core_positions {
        mask[p] = false;
        let out = verifier.solve(formula, &mask, limits, cancel);
        calls += 1;
        mask[p] = true;
        member_evidence.push(MemberEvidence {
            member: formula.clauses[p].id.clone(),
            status: out.status,
            witness_model: out.model,
            reason: out.reason,
        });
        if out.status == SolveStatus::Unsat {
            return VerificationReport {
                verdict: Verdict::NotMinimal,
                verifier_solver: name,
                whole_status: whole.status,
                member_evidence,
                note: Some(format!(
                    "removing member '{}' leaves the formula unsatisfiable, so it is redundant inside the core",
                    formula.clauses[p].id
                )),
                verifier_calls: calls,
            };
        }
        if out.status == SolveStatus::Unknown {
            inconclusive = true;
        }
    }

    if whole.status == SolveStatus::Unknown || inconclusive {
        VerificationReport {
            verdict: Verdict::Inconclusive,
            verifier_solver: name,
            whole_status: whole.status,
            member_evidence,
            note: Some(
                "verifier could not decide one or more required SAT/UNSAT checks; \
                 core is neither accepted nor rejected"
                    .to_string(),
            ),
            verifier_calls: calls,
        }
    } else {
        VerificationReport {
            verdict: Verdict::Certified,
            verifier_solver: name,
            whole_status: whole.status,
            member_evidence,
            note: None,
            verifier_calls: calls,
        }
    }
}

#[cfg(test)]
mod tests {
    use crate::language::parse_dimacs;
    use crate::solver::brute::BruteForceSolver;
    use crate::solver::SolveLimits;

    use super::*;

    #[test]
    fn certifies_x_and_not_x() {
        let f = parse_dimacs("p cnf 1 2\n1 0\n-1 0\n").unwrap();
        let v = BruteForceSolver::default();
        let report = certify(&f, &[0, 1], &v, &SolveLimits::default(), None);
        assert_eq!(report.verdict, Verdict::Certified);
        assert_eq!(report.verifier_calls, 3);
        assert!(report
            .member_evidence
            .iter()
            .all(|e| e.status == SolveStatus::Sat));
    }

    #[test]
    fn rejects_non_minimal_core() {
        // {x, -x, y}: the third clause is redundant inside the core.
        let f = parse_dimacs("p cnf 2 3\n1 0\n-1 0\n2 0\n").unwrap();
        let v = BruteForceSolver::default();
        let report = certify(&f, &[0, 1, 2], &v, &SolveLimits::default(), None);
        assert_eq!(report.verdict, Verdict::NotMinimal);
    }

    #[test]
    fn rejects_satisfiable_claimed_core() {
        let f = parse_dimacs("p cnf 1 1\n1 0\n").unwrap();
        let v = BruteForceSolver::default();
        let report = certify(&f, &[0], &v, &SolveLimits::default(), None);
        assert_eq!(report.verdict, Verdict::CoreNotUnsat);
    }
}
