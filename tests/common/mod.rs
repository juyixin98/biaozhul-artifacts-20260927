// Test-only shared harness: helpers here are used from different integration-test
// binaries, so not every item appears used in each one.
#![allow(dead_code, unused_imports)]

//! Test-only infrastructure.
//!
//! The [`oracle`] here is a THIRD SAT implementation, written independently inside the
//! tests tree: it shares no code with the in-crate DPLL or brute-force verifier. Test
//! expected answers come from (a) hand-specified clause-id sets in the test bodies and
//! (b) this independent enumeration oracle — never from the extraction implementation
//! under test. That keeps the suite honest: the service cannot pass by certifying its
//! own output with its own reasoning.

pub mod fixtures;
pub mod oracle;

use std::sync::atomic::AtomicBool;
use std::sync::Arc;

use mus_service::language::{Clause, ClauseId, Formula, Literal};
use mus_service::solver::{SolveLimits, SolveOutcome, SolveStatus, SatSolver};

pub use mus_service::api::AppState;
pub use mus_service::config::Config;
pub use mus_service::core::{
    extract, ExtractionConfig, ExtractionReport, OrderPolicy, Outcome, ProofState,
};
pub use mus_service::solver::registry::SolverRegistry;

/// Build a clause from an id and DIMACS-style integer literals.
pub fn clause(id: &str, lits: &[i64]) -> Clause {
    Clause {
        id: ClauseId::new(id),
        literals: lits
            .iter()
            .map(|n| Literal::from_dimacs(*n).unwrap())
            .collect(),
        sensitive: false,
    }
}

pub fn sensitive_clause(id: &str, lits: &[i64]) -> Clause {
    let mut c = clause(id, lits);
    c.sensitive = true;
    c
}

/// Default extraction config for tests (verification handled explicitly per test).
pub fn test_cfg() -> ExtractionConfig {
    ExtractionConfig {
        order: OrderPolicy::Input,
        max_solver_calls: None,
        solve_limits: SolveLimits {
            max_decisions: Some(500_000),
            timeout_ms: Some(5_000),
        },
        verify: false,
    }
}

/// Solver whose responses are scripted by a closure — lets tests force UNKNOWN,
/// cancellations and other boundary behaviour without touching real search.
pub struct ScriptedSolver<F>
where
    F: Fn(usize, &[bool]) -> SolveOutcome + Send + Sync,
{
    pub name: String,
    pub calls: std::sync::atomic::AtomicUsize,
    pub behavior: F,
}

impl<F> ScriptedSolver<F>
where
    F: Fn(usize, &[bool]) -> SolveOutcome + Send + Sync,
{
    pub fn new(name: &str, behavior: F) -> Self {
        Self {
            name: name.to_string(),
            calls: std::sync::atomic::AtomicUsize::new(0),
            behavior,
        }
    }
}

impl<F> SatSolver for ScriptedSolver<F>
where
    F: Fn(usize, &[bool]) -> SolveOutcome + Send + Sync,
{
    fn name(&self) -> &str {
        &self.name
    }

    fn solve(
        &self,
        _formula: &Formula,
        mask: &[bool],
        _limits: &SolveLimits,
        _cancel: Option<&AtomicBool>,
    ) -> SolveOutcome {
        let n = self
            .calls
            .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        (self.behavior)(n, mask)
    }
}

/// Solver that runs real DPLL, but after `trials_before_cancel` deletion trials have
/// completed it raises the external cancel flag. The initial whole-input check is
/// always answered normally (and genuinely), so the input is proved UNSAT first and
/// any subsequent cancellation is exercised during the deletion phase.
pub struct CancelAfterSolver {
    pub inner: mus_service::solver::dpll::DpllSolver,
    pub flag: Arc<AtomicBool>,
    /// 0 = cancel right after the initial check; k = cancel after k trials complete.
    pub trials_before_cancel: usize,
    trials_seen: std::sync::atomic::AtomicUsize,
}

impl CancelAfterSolver {
    pub fn new(flag: Arc<AtomicBool>, trials_before_cancel: usize) -> Self {
        Self {
            inner: mus_service::solver::dpll::DpllSolver::new(),
            flag,
            trials_before_cancel,
            trials_seen: std::sync::atomic::AtomicUsize::new(0),
        }
    }
}

impl SatSolver for CancelAfterSolver {
    fn name(&self) -> &str {
        "cancel-after"
    }

    fn solve(
        &self,
        formula: &Formula,
        mask: &[bool],
        limits: &SolveLimits,
        cancel: Option<&AtomicBool>,
    ) -> SolveOutcome {
        let is_initial_check = mask.iter().all(|b| *b);
        let out = self.inner.solve(formula, mask, limits, cancel);
        if !is_initial_check {
            let done = self
                .trials_seen
                .fetch_add(1, std::sync::atomic::Ordering::SeqCst)
                + 1;
            if done >= self.trials_before_cancel {
                self.flag
                    .store(true, std::sync::atomic::Ordering::SeqCst);
            }
        }
        out
    }
}

/// Assert the exact id set of the returned core, in formula order.
pub fn assert_core_ids(report: &ExtractionReport, expected: &[&str]) {
    let got: Vec<&str> = report.core.iter().map(|id| id.0.as_str()).collect();
    assert_eq!(got, expected, "core id set/order mismatch");
}

/// Every member must carry the strong per-member proof state on a completed run.
pub fn assert_all_members_witnessed(report: &ExtractionReport) {
    for m in &report.member_proofs {
        assert!(
            matches!(m.state, ProofState::SatWitness | ProofState::InitialCheck),
            "member {} has weak proof state {:?}",
            m.id.0,
            m.state
        );
    }
}

pub fn completed_outcomes() -> [Outcome; 2] {
    [Outcome::Completed, Outcome::CompletedWithUnknownTrials]
}

pub fn status_is(_s: SolveStatus) {
    // imported for convenience in tests
}
