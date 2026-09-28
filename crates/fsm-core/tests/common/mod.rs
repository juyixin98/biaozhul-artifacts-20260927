//! Shared helpers for integration tests. Fixture loading and an
//! independently-written recursive reachability oracle.

use fsm_core::Budget;
use fsm_lang::compile::CompiledSpec;
use fsm_lang::evidence::NamedValue;
use fsm_lang::model::Value;
use fsm_lang::Spec;
use std::collections::HashSet;

pub mod independent_oracle {
    use super::*;

    /// Recursive DFS reachability written independently of the kernel's
    /// BFS: own visited set, own recursion, using only the compiled
    /// language primitives (guard evaluation + simultaneous apply).
    pub fn reachable_states(spec: &CompiledSpec) -> HashSet<Vec<Value>> {
        let mut seen = HashSet::new();
        for state in spec.iter_domain() {
            if spec.initial.eval_bool(&state).ok() == Some(true) {
                walk(spec, state, &mut seen);
            }
        }
        seen
    }

    fn walk(spec: &CompiledSpec, state: Vec<Value>, seen: &mut HashSet<Vec<Value>>) {
        if !seen.insert(state.clone()) {
            return;
        }
        let is_terminal = spec
            .terminal
            .as_ref()
            .and_then(|t| t.eval_bool(&state).ok())
            .unwrap_or(false);
        if is_terminal {
            return;
        }
        for tr in &spec.transitions {
            if tr.guard.eval_bool(&state).ok() != Some(true) {
                continue;
            }
            if let Ok(next) = spec.apply(tr, &state) {
                walk(spec, next, seen);
            }
        }
    }
}

/// Load a compiled specification from `tests/../../fixtures/<name>.json`.
pub fn load_fixture(name: &str) -> CompiledSpec {
    let path = fixture_path(name);
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("read fixture {path}: {e}"));
    let model: Spec = serde_json::from_str(&text)
        .unwrap_or_else(|e| panic!("parse fixture {path}: {e}"));
    CompiledSpec::compile(&model).unwrap_or_else(|e| panic!("compile fixture {path}: {e}"))
}

pub fn fixture_path(name: &str) -> String {
    format!("{}/../../fixtures/{name}.json", env!("CARGO_MANIFEST_DIR"))
}

/// Generous budget for fixtures that should explore fully.
pub fn spec_budget(_spec: &CompiledSpec) -> Budget {
    Budget {
        max_states: 100_000,
        max_transitions: 1_000_000,
        max_initial_scan: 1_000_000,
    }
}

/// Look up a named value within an evidence trace state.
pub fn named<'a>(state: &'a [NamedValue], var: &str) -> &'a Value {
    &state
        .iter()
        .find(|nv| nv.var == var)
        .unwrap_or_else(|| panic!("variable {var} absent from trace state"))
        .value
}
