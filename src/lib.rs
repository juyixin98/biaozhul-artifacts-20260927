//! Subset-minimal unsatisfiable core extraction service.
//!
//! Crate layout (each module has a real responsibility):
//!
//! * [`language`] — input language, constraint identity, CNF parsing/validation.
//! * [`solver`]   — pluggable SAT-solver boundary (`Solver` trait), budget/cancel,
//!   built-in DPLL, external DIMACS CLI adapter, independent oracle.
//! * [`extract`]  — deletion-based subset-minimal core extraction with audit trace.
//! * [`verify`]   — independent evidence verification (core UNSAT + minimality +
//!   witness checking + trace audit).
//! * [`api`]      — Axum HTTP transport, DTOs, async jobs.
//! * [`config`]   — file/env configuration.
//! * [`diagnostics`] — correlation ids and sensitive-data redaction.

pub mod api;
pub mod config;
pub mod diagnostics;
pub mod extract;
pub mod language;
pub mod solver;
pub mod verify;

pub use api::AppState;
use solver::{build_solver, Solver, SolverSpec};
use std::sync::Arc;
use tokio::sync::Semaphore;

/// Assemble application state from configuration. Fails if the configured primary
/// external solver cannot be resolved.
pub fn build_state(cfg: config::Config) -> Result<AppState, String> {
    let primary_spec = match cfg.solver_kind.as_str() {
        "builtin" => SolverSpec::Builtin,
        "external" => {
            let binary = cfg
                .external_binary
                .clone()
                .ok_or_else(|| "solver_kind=external requires external_binary".to_string())?;
            SolverSpec::External { binary, args: cfg.external_args.clone() }
        }
        other => return Err(format!("unknown solver_kind {other:?}")),
    };
    let primary: Arc<dyn Solver> = build_solver(&primary_spec)?;

    // The independent oracle is a *different implementation* from the primary.
    // When the primary is the built-in DPLL we use brute-force truth tables;
    // when the primary is external (or brute-force is too small) we use DPLL.
    let oracle: Arc<dyn Solver> = match cfg.solver_kind.as_str() {
        "builtin" => Arc::new(solver::oracle::BruteForceSolver::new()),
        _ => Arc::new(solver::builtin::DpllSolver::default()),
    };

    let log_formulas = cfg.log_formulas;
    Ok(AppState {
        cfg: Arc::new(cfg),
        primary,
        oracle,
        jobs: Default::default(),
        cancels: Default::default(),
        redactor: diagnostics::Redactor::new(log_formulas),
        concurrency: Arc::new(Semaphore::new(8)),
    })
}
