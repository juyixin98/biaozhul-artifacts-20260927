//! mus-service: subset-minimal unsatisfiable core extraction with replaceable solvers.
//!
//! Module responsibilities:
//! - [`language`]: CNF input language and constraint identity contract
//! - [`solver`]: solver trait, DPLL/brute-force/external implementations, registry
//! - [`evidence`]: independent core certification (whole UNSAT + per-member SAT)
//! - [`core`]: deletion-based extraction state machine (budget/cancel/UNKNOWN handling)
//! - [`diag`]: auditable decision records with redaction
//! - [`config`]: file + environment configuration
//! - [`api`]: Axum HTTP layer (sync endpoint + cancellable jobs)

pub mod api;
pub mod config;
pub mod core;
pub mod diag;
pub mod evidence;
pub mod language;
pub mod solver;

pub use api::router;
pub use api::AppState;
pub use config::Config;
pub use solver::registry::SolverRegistry;

/// Construct application state from configuration, wiring solver names to instances.
pub fn build_state(config: Config) -> AppState {
    let brute_vars = config.brute_max_vars;
    let registry = SolverRegistry::from_specs(&config.solvers, brute_vars);
    let names = registry.names();
    if !names.contains(&config.default_solver) {
        tracing::warn!(
            default_solver = %config.default_solver,
            available = ?names,
            "configured default solver is not registered; requests will get an explicit error"
        );
    }
    AppState::new(
        std::sync::Arc::new(config),
        std::sync::Arc::new(registry),
    )
}
