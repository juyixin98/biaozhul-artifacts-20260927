//! Independent evidence replay.
//!
//! This crate deliberately does NOT call the kernel (`fsm-core`). Given a
//! compiled specification and a serialized [`Evidence`] trace, it rebuilds
//! each state from the named assignments and checks, step by step:
//!
//! 1. the trace is non-empty and every state assigns every declared
//!    variable exactly once with in-domain values;
//! 2. the root state satisfies the initial predicate;
//! 3. every fired transition exists, its guard held in the pre-state, and
//!    applying its simultaneous update to the pre-state reproduces the
//!    claimed post-state exactly (no missing/extra changes);
//! 4. the final state satisfies the claim implied by the evidence kind
//!    (AG violation / EF witness), or genuinely deadlocks / legally
//!    terminates.
//!
//! A replay report records every step so a counterexample can be
//! hand-replayed from the API output.

use fsm_lang::compile::CompiledSpec;
use fsm_lang::error::EvalError;
use fsm_lang::evidence::{kind, Evidence, NamedValue};
use fsm_lang::model::Value;
use serde::{Deserialize, Serialize};

/// Replay failure codes.
pub mod fail {
    pub const EMPTY_TRACE: &str = "EMPTY_TRACE";
    pub const BAD_STATE: &str = "BAD_STATE";
    pub const ROOT_NOT_INITIAL: &str = "ROOT_NOT_INITIAL";
    pub const MISSING_FIRED: &str = "MISSING_FIRED";
    pub const SPURIOUS_FIRED: &str = "SPURIOUS_FIRED";
    pub const UNKNOWN_TRANSITION: &str = "UNKNOWN_TRANSITION";
    pub const GUARD_FALSE: &str = "GUARD_FALSE";
    pub const UPDATE_MISMATCH: &str = "UPDATE_MISMATCH";
    pub const FINAL_NOT_VIOLATION: &str = "FINAL_NOT_VIOLATION";
    pub const FINAL_NOT_WITNESS: &str = "FINAL_NOT_WITNESS";
    pub const FINAL_NOT_DEADLOCK: &str = "FINAL_NOT_DEADLOCK";
    pub const FINAL_NOT_TERMINAL: &str = "FINAL_NOT_TERMINAL";
    pub const UNKNOWN_KIND: &str = "UNKNOWN_KIND";
    pub const PROPERTY_NOT_FOUND: &str = "PROPERTY_NOT_FOUND";
    pub const EVALUATION_ERROR: &str = "EVALUATION_ERROR";
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReplayStep {
    pub index: usize,
    pub fired: Option<String>,
    pub state: Vec<NamedValue>,
    /// Human-readable check outcome for this step.
    pub check: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReplayFailure {
    pub code: String,
    pub message: String,
    /// Trace index at which the failure was detected, when applicable.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub at_index: Option<usize>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReplayReport {
    pub valid: bool,
    pub evidence_kind: String,
    pub property: String,
    pub steps: Vec<ReplayStep>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failure: Option<ReplayFailure>,
}

fn fail_report(
    evidence: &Evidence,
    steps: Vec<ReplayStep>,
    code: &str,
    at_index: Option<usize>,
    message: impl Into<String>,
) -> ReplayReport {
    ReplayReport {
        valid: false,
        evidence_kind: evidence.kind.clone(),
        property: evidence.property.clone(),
        steps,
        failure: Some(ReplayFailure {
            code: code.to_string(),
            message: message.into(),
            at_index,
        }),
    }
}

/// Convert a named assignment to a positional state vector in declaration
/// order, checking for missing/duplicate/unknown variables and domain
/// membership.
fn materialize(
    spec: &CompiledSpec,
    named: &[NamedValue],
    index: usize,
) -> Result<Vec<Value>, ReplayFailure> {
    use std::collections::HashSet;
    if named.len() != spec.width() {
        return Err(ReplayFailure {
            code: fail::BAD_STATE.to_string(),
            message: format!(
                "state {} assigns {} variables, expected {}",
                index,
                named.len(),
                spec.width()
            ),
            at_index: Some(index),
        });
    }
    let mut seen: HashSet<String> = HashSet::new();
    let mut state = vec![
        Value::Bool(false);
        spec.width()
    ];
    for nv in named {
        if !seen.insert(nv.var.clone()) {
            return Err(ReplayFailure {
                code: fail::BAD_STATE.to_string(),
                message: format!("state {} assigns `{}` more than once", index, nv.var),
                at_index: Some(index),
            });
        }
        let pos = match spec.variables.iter().position(|v| v.name == nv.var) {
            Some(p) => p,
            None => {
                return Err(ReplayFailure {
                    code: fail::BAD_STATE.to_string(),
                    message: format!("state {} names unknown variable `{}`", index, nv.var),
                    at_index: Some(index),
                })
            }
        };
        if !spec.variables[pos].domain.contains(&nv.value) {
            return Err(ReplayFailure {
                code: fail::BAD_STATE.to_string(),
                message: format!(
                    "state {}: value {} for `{}` is outside its declared domain",
                    index, nv.value, nv.var
                ),
                at_index: Some(index),
            });
        }
        state[pos] = nv.value.clone();
    }
    Ok(state)
}

fn eval_err_failure(e: EvalError, index: usize, what: &str) -> ReplayFailure {
    ReplayFailure {
        code: fail::EVALUATION_ERROR.to_string(),
        message: format!("{}: {}: {}", what, e.code, e.message),
        at_index: Some(index),
    }
}

/// Independently replay an evidence trace.
pub fn replay(spec: &CompiledSpec, evidence: &Evidence) -> ReplayReport {
    if evidence.trace.is_empty() {
        return fail_report(
            evidence,
            vec![],
            fail::EMPTY_TRACE,
            None,
            "evidence contains no states",
        );
    }

    let mut steps = Vec::with_capacity(evidence.trace.len());
    let mut states: Vec<Vec<Value>> = Vec::with_capacity(evidence.trace.len());

    for ts in &evidence.trace {
        match materialize(spec, &ts.state, ts.index) {
            Ok(state) => states.push(state),
            Err(f) => return fail_report(evidence, steps, &f.code, f.at_index, f.message),
        }
        steps.push(ReplayStep {
            index: ts.index,
            fired: ts.fired.clone(),
            state: ts.state.clone(),
            check: String::new(),
        });
    }

    // Root must be an initial state.
    match spec.initial.eval_bool(&states[0]) {
        Ok(true) => steps[0].check = "initial predicate holds".to_string(),
        Ok(false) => {
            return fail_report(
                evidence,
                steps,
                fail::ROOT_NOT_INITIAL,
                Some(0),
                "root state does not satisfy the initial predicate",
            )
        }
        Err(e) => {
            let f = eval_err_failure(e, 0, "initial predicate");
            return fail_report(evidence, steps, &f.code, f.at_index, f.message);
        }
    }

    // The root's `fired` must be null.
    if evidence.trace[0].fired.is_some() {
        return fail_report(
            evidence,
            steps,
            fail::SPURIOUS_FIRED,
            Some(0),
            "root state must have fired = null",
        );
    }

    // Walk every transition of the trace independently.
    for i in 1..states.len() {
        let ts = &evidence.trace[i];
        let fired = match &ts.fired {
            Some(name) => name,
            None => {
                return fail_report(
                    evidence,
                    steps,
                    fail::MISSING_FIRED,
                    Some(i),
                    format!("state {i} is not the root but names no fired transition"),
                )
            }
        };
        let transition = match spec.transitions.iter().find(|t| t.name == *fired) {
            Some(t) => t,
            None => {
                return fail_report(
                    evidence,
                    steps,
                    fail::UNKNOWN_TRANSITION,
                    Some(i),
                    format!("unknown transition `{fired}`"),
                )
            }
        };
        let pre = &states[i - 1];
        let guard = match transition.guard.eval_bool(pre) {
            Ok(g) => g,
            Err(e) => {
                let f = eval_err_failure(e, i, &format!("guard of `{fired}`"));
                return fail_report(evidence, steps, &f.code, f.at_index, f.message);
            }
        };
        if !guard {
            return fail_report(
                evidence,
                steps,
                fail::GUARD_FALSE,
                Some(i),
                format!("guard of `{fired}` was false in the pre-state"),
            );
        }
        let computed = match spec.apply(transition, pre) {
            Ok(s) => s,
            Err(e) => {
                return fail_report(
                    evidence,
                    steps,
                    &e.code,
                    Some(i),
                    format!("firing `{fired}`: {}", e.message),
                )
            }
        };
        if computed != states[i] {
            return fail_report(
                evidence,
                steps,
                fail::UPDATE_MISMATCH,
                Some(i),
                format!(
                    "firing `{fired}` does not reproduce the claimed state from the pre-state"
                ),
            );
        }
        steps[i].check = format!("guard held; `{fired}` reproduces this state");
    }

    // Verify the final-state claim.
    let last = states.len() - 1;
    let final_state = &states[last];
    match evidence.kind.as_str() {
        kind::AG_VIOLATION | kind::EF_WITNESS => {
            let prop = match spec
                .properties
                .iter()
                .find(|p| p.name == evidence.property)
            {
                Some(p) => p,
                None => {
                    return fail_report(
                        evidence,
                        steps,
                        fail::PROPERTY_NOT_FOUND,
                        Some(last),
                        format!("property `{}` not found in specification", evidence.property),
                    )
                }
            };
            let holds = match prop.predicate.eval_bool(final_state) {
                Ok(h) => h,
                Err(e) => {
                    let f = eval_err_failure(e, last, "property predicate");
                    return fail_report(evidence, steps, &f.code, f.at_index, f.message);
                }
            };
            if evidence.kind == kind::AG_VIOLATION {
                if holds {
                    return fail_report(
                        evidence,
                        steps,
                        fail::FINAL_NOT_VIOLATION,
                        Some(last),
                        "final state satisfies the predicate, so it is not an AG violation",
                    );
                }
                steps[last].check =
                    "predicate false in final state: genuine AG violation".to_string();
            } else if !holds {
                return fail_report(
                    evidence,
                    steps,
                    fail::FINAL_NOT_WITNESS,
                    Some(last),
                    "final state does not satisfy the EF target predicate",
                );
            } else {
                steps[last].check =
                    "predicate true in final state: genuine EF witness".to_string();
            }
        }
        kind::DEADLOCK => {
            if let Some(term) = &spec.terminal {
                match term.eval_bool(final_state) {
                    Ok(true) => {
                        return fail_report(
                            evidence,
                            steps,
                            fail::FINAL_NOT_DEADLOCK,
                            Some(last),
                            "final state satisfies the terminal predicate; it is a legal \
                             termination, not a deadlock",
                        )
                    }
                    Ok(false) => {}
                    Err(e) => {
                        let f = eval_err_failure(e, last, "terminal predicate");
                        return fail_report(evidence, steps, &f.code, f.at_index, f.message);
                    }
                }
            }
            for t in &spec.transitions {
                match t.guard.eval_bool(final_state) {
                    Ok(true) => {
                        return fail_report(
                            evidence,
                            steps,
                            fail::FINAL_NOT_DEADLOCK,
                            Some(last),
                            format!("transition `{}` is enabled; state is not a deadlock", t.name),
                        )
                    }
                    Ok(false) => {}
                    Err(e) => {
                        let f = eval_err_failure(e, last, &format!("guard of `{}`", t.name));
                        return fail_report(evidence, steps, &f.code, f.at_index, f.message);
                    }
                }
            }
            steps[last].check = "no transition enabled and not legal-terminal: genuine deadlock"
                .to_string();
        }
        kind::TERMINAL => {
            match &spec.terminal {
                None => {
                    return fail_report(
                        evidence,
                        steps,
                        fail::FINAL_NOT_TERMINAL,
                        Some(last),
                        "specification declares no terminal predicate",
                    )
                }
                Some(term) => match term.eval_bool(final_state) {
                    Ok(true) => {
                        steps[last].check =
                            "terminal predicate holds: genuine legal termination".to_string()
                    }
                    Ok(false) => {
                        return fail_report(
                            evidence,
                            steps,
                            fail::FINAL_NOT_TERMINAL,
                            Some(last),
                            "final state does not satisfy the terminal predicate",
                        )
                    }
                    Err(e) => {
                        let f = eval_err_failure(e, last, "terminal predicate");
                        return fail_report(evidence, steps, &f.code, f.at_index, f.message);
                    }
                },
            }
        }
        other => {
            return fail_report(
                evidence,
                steps,
                fail::UNKNOWN_KIND,
                Some(last),
                format!("unknown evidence kind `{other}`"),
            )
        }
    }

    ReplayReport {
        valid: true,
        evidence_kind: evidence.kind.clone(),
        property: evidence.property.clone(),
        steps,
        failure: None,
    }
}
