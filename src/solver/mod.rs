//! Solver kernel: the pluggable SAT-solver boundary.
//!
//! Anything that decides satisfiability implements [`Solver`]. The extraction and
//! verification layers depend only on this trait, so backends are replaceable:
//!
//! * [`builtin::DpllSolver`] — dependency-free built-in DPLL (default).
//! * [`external::ExternalCliSolver`] — any local DIMACS-speaking CLI binary
//!   (e.g. a system minisat/dpll), configured by path/argv.
//! * Test kernels in `tests/common` (scripted SAT/UNSAT/UNKNOWN, stalls).
//!
//! Hard rule enforced by the types here: an [`SStatus::Unknown`] answer is a third
//! outcome, never silently treated as UNSAT.

pub mod builtin;
pub mod external;
pub mod oracle;

use crate::language::{Cnf, Model};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

/// Outcome of a satisfiability query. `Unknown` means "cannot decide" (resource
/// limit, timeout, backend failure) — it is distinct from both `Sat` and `Unsat`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SStatus {
    Sat,
    Unsat,
    Unknown,
}

#[derive(Debug, Clone)]
pub struct SolveResult {
    pub status: SStatus,
    /// Present (and 1-based indexed) exactly when `status == Sat`.
    pub model: Option<Model>,
    /// Free-form backend detail, used in diagnostics.
    pub detail: Option<String>,
}

impl SolveResult {
    #[must_use]
    pub fn sat(model: Model) -> Self {
        Self { status: SStatus::Sat, model: Some(model), detail: None }
    }
    #[must_use]
    pub fn unsat() -> Self {
        Self { status: SStatus::Unsat, model: None, detail: None }
    }
    #[must_use]
    pub fn unknown(detail: impl Into<String>) -> Self {
        Self { status: SStatus::Unknown, model: None, detail: Some(detail.into()) }
    }
}

/// Shared, cheaply-clonable solver-call budget. Every backend increments
/// [`Budget::tick`] once per *attempted* decision and must observe exhaustion.
#[derive(Debug, Clone, Default)]
pub struct Budget {
    inner: Arc<BudgetInner>,
}

#[derive(Debug, Default)]
struct BudgetInner {
    limit: AtomicU64,
    used: AtomicU64,
}

impl Budget {
    /// `0` means unlimited.
    #[must_use]
    pub fn new(limit: u64) -> Self {
        let b = Budget::default();
        b.inner.limit.store(limit, Ordering::SeqCst);
        b
    }

    /// Count one solver call. Returns `false` if the budget is already exhausted
    /// (the caller must then stop issuing decisions).
    #[must_use]
    pub fn tick(&self) -> bool {
        let used = self.inner.used.fetch_add(1, Ordering::SeqCst) + 1;
        let limit = self.inner.limit.load(Ordering::SeqCst);
        limit == 0 || used <= limit
    }

    #[must_use]
    pub fn used(&self) -> u64 {
        self.inner.used.load(Ordering::SeqCst)
    }

    #[must_use]
    pub fn limit(&self) -> u64 {
        self.inner.limit.load(Ordering::SeqCst)
    }

    #[must_use]
    pub fn remaining(&self) -> Option<u64> {
        let limit = self.limit();
        if limit == 0 {
            None
        } else {
            Some(limit.saturating_sub(self.used()))
        }
    }

    #[must_use]
    pub fn exhausted(&self) -> bool {
        matches!(self.remaining(), Some(0))
    }
}

/// Cooperative cancellation flag, checked between solver calls (and inside the
/// built-in search loop).
#[derive(Debug, Clone, Default)]
pub struct CancelToken {
    inner: Arc<AtomicBool>,
}

impl CancelToken {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }
    pub fn cancel(&self) {
        self.inner.store(true, Ordering::SeqCst);
    }
    #[must_use]
    pub fn is_cancelled(&self) -> bool {
        self.inner.load(Ordering::SeqCst)
    }
}

/// Context every solver call receives.
#[derive(Debug, Clone)]
pub struct SolveCtx {
    pub budget: Budget,
    pub cancel: CancelToken,
}

impl SolveCtx {
    #[must_use]
    pub fn new(budget: Budget, cancel: CancelToken) -> Self {
        Self { budget, cancel }
    }
}

/// The replaceable SAT-solver interface.
pub trait Solver: Send + Sync {
    /// Human-readable backend identifier used in traces/diagnostics (no secrets).
    fn name(&self) -> &str;

    /// Decide `cnf`. Implementations MUST:
    /// * call `ctx.budget.tick()` exactly once per attempted decision and treat a
    ///   `false` return as [`SStatus::Unknown`] ("budget exhausted"), never as UNSAT;
    /// * return [`SStatus::Unknown`] when `ctx.cancel` is observed;
    /// * only attach a model for a genuine `Sat`.
    fn solve(&self, cnf: &Cnf, ctx: &SolveCtx) -> SolveResult;
}

/// Backend configuration resolved by the registry.
#[derive(Debug, Clone)]
pub enum SolverSpec {
    Builtin,
    External { binary: String, args: Vec<String> },
}

/// Build the configured primary solver, or explain why it cannot be built.
pub fn build_solver(spec: &SolverSpec) -> Result<Arc<dyn Solver>, String> {
    match spec {
        SolverSpec::Builtin => Ok(Arc::new(builtin::DpllSolver::default())),
        SolverSpec::External { binary, args } => Ok(Arc::new(external::ExternalCliSolver::new(
            binary.clone(),
            args.clone(),
        ))),
    }
}
