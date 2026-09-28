//! Solver registry: named, configuration-constructed solver instances.
//!
//! Extraction and verification each reference a solver *by name*. The built-in names
//! `dpll` and `brute` are always present; additional external solvers can be declared
//! in configuration (`[[solvers]]`). Replacing the production solver therefore means
//! editing config (or naming a different solver per request), never editing the
//! extraction algorithm.

use std::collections::BTreeMap;
use std::sync::Arc;

use super::brute::BruteForceSolver;
use super::dpll::DpllSolver;
use super::external::ExternalSolver;
use super::SatSolver;

/// Declarative description of one solver, as parsed from config.
#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SolverSpec {
    Dpll {
        #[serde(default)]
        var_cap: usize,
    },
    Brute {
        #[serde(default = "default_brute_vars")]
        max_vars: usize,
    },
    External {
        #[serde(default)]
        name: String,
        argv: Vec<String>,
    },
}

fn default_brute_vars() -> usize {
    22
}

#[derive(Clone)]
pub struct SolverRegistry {
    solvers: BTreeMap<String, Arc<dyn SatSolver>>,
}

impl std::fmt::Debug for SolverRegistry {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SolverRegistry")
            .field("solvers", &self.solvers.keys().collect::<Vec<_>>())
            .finish()
    }
}

impl SolverRegistry {
    /// Built-in solvers only.
    pub fn built_in(brute_vars: usize) -> Self {
        let mut r = Self {
            solvers: BTreeMap::new(),
        };
        r.register(Arc::new(DpllSolver::new()));
        r.register(Arc::new(BruteForceSolver::new(brute_vars)));
        r
    }

    /// Build from configured specs; the built-in `dpll`/`brute` names are always
    /// available and are only replaced if a spec explicitly claims the name.
    pub fn from_specs(specs: &[SolverSpec], brute_max_vars: usize) -> Self {
        let mut reg = Self::built_in(brute_max_vars);
        for spec in specs {
            match spec {
                SolverSpec::Dpll { var_cap } => {
                    let cap = if *var_cap == 0 {
                        usize::MAX
                    } else {
                        *var_cap
                    };
                    reg.register(Arc::new(DpllSolver { var_cap: cap }));
                }
                SolverSpec::Brute { max_vars } => {
                    reg.register(Arc::new(BruteForceSolver::new(*max_vars)));
                }
                SolverSpec::External { name, argv } => {
                    let nm = if name.is_empty() { "external" } else { name };
                    reg.register(Arc::new(ExternalSolver::new(nm, argv.clone())));
                }
            }
        }
        reg
    }

    pub fn register(&mut self, solver: Arc<dyn SatSolver>) {
        self.solvers.insert(solver.name().to_string(), solver);
    }

    pub fn get(&self, name: &str) -> Option<Arc<dyn SatSolver>> {
        self.solvers.get(name).cloned()
    }

    pub fn names(&self) -> Vec<String> {
        self.solvers.keys().cloned().collect()
    }

    /// Inject a solver directly (used by tests with scripted/stub solvers).
    pub fn insert_custom(&mut self, solver: Arc<dyn SatSolver>) {
        self.register(solver);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builtin_names_present() {
        let reg = SolverRegistry::built_in(20);
        assert!(reg.get("dpll").is_some());
        assert!(reg.get("brute").is_some());
        assert!(reg.get("nope").is_none());
    }

    #[test]
    fn external_spec_registers_named_solver() {
        let specs = vec![SolverSpec::External {
            name: "kissat".into(),
            argv: vec!["kissat".into(), "{in}".into()],
        }];
        let reg = SolverRegistry::from_specs(&specs, default_brute_vars());
        let s = reg.get("kissat").expect("registered");
        assert_eq!(s.name(), "kissat");
    }
}
