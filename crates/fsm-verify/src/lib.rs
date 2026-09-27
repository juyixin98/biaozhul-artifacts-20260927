//! # fsm-verify
//!
//! Independent evidence replayer. It takes a witness produced by `fsm-core`
//! and re-derives every claim from the specification alone, never calling the
//! search kernel:
//!
//! 1. the first state satisfies the init predicate,
//! 2. each consecutive pair is one real transition — guard enabled in the
//!    pre-state and parallel update reproduces the claimed post-state exactly,
//! 3. the final state demonstrates the claimed conclusion (invariant false,
//!    EF target true, or deadlocked & non-terminal).
//!
//! A single mismatch produces a structured failure code. The blackbox tests
//! tamper with valid witnesses to confirm each check is live.

use serde::{Deserialize, Serialize};

use fsm_lang::parser::parse_expr;
use fsm_lang::system::{Domain, Value};
use fsm_lang::System;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EvidenceKindInput {
    AgViolation,
    EfReachable,
    Deadlock,
}

#[derive(Debug, Clone, Deserialize)]
pub struct StepInput {
    #[serde(default)]
    pub state: serde_json::Map<String, serde_json::Value>,
    #[serde(default)]
    pub fired: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct EvidenceInput {
    pub kind: EvidenceKindInput,
    /// Property expression for AG/EF evidence; ignored for deadlock.
    #[serde(default)]
    pub expr: Option<String>,
    pub path: Vec<StepInput>,
    /// Declared number of transitions; must equal `path.len() - 1`.
    #[serde(default)]
    pub length: Option<usize>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FailCode {
    EmptyPath,
    InvalidPropertyExpr,
    StateMissingVariable,
    StateUnknownVariable,
    StateValueTypeMismatch,
    StateValueOutOfDomain,
    FirstStepFired,
    LaterStepNotFired,
    UnknownTransition,
    InitialStateNotSelected,
    GuardDisabled,
    UpdateMismatch,
    FinalInvariantHolds,
    FinalTargetFalse,
    FinalNotDeadlocked,
    FinalTerminal,
    LengthFieldMismatch,
}

#[derive(Debug, Clone, Serialize)]
pub struct Failure {
    pub code: FailCode,
    pub detail: String,
    pub step: Option<usize>,
}

#[derive(Debug, Clone, Serialize)]
pub struct VerificationReport {
    pub accepted: bool,
    /// Number of replayed transitions.
    pub replayed_steps: usize,
    pub failures: Vec<Failure>,
}

impl VerificationReport {
    fn ok(replayed: usize) -> Self {
        VerificationReport {
            accepted: true,
            replayed_steps: replayed,
            failures: Vec::new(),
        }
    }
}

fn fail(code: FailCode, step: Option<usize>, detail: impl Into<String>) -> Failure {
    Failure {
        code,
        detail: detail.into(),
        step,
    }
}

/// Verify one evidence object against a fully built system.
pub fn verify(sys: &System, ev: &EvidenceInput) -> VerificationReport {
    let mut failures = Vec::new();

    let property_expr = match ev.kind {
        EvidenceKindInput::AgViolation | EvidenceKindInput::EfReachable => {
            let src = ev.expr.as_ref().filter(|s| !s.trim().is_empty());
            match src {
                Some(src) => {
                    let mut e = match parse_expr(src) {
                        Ok(e) => e,
                        Err(e) => {
                            failures.push(fail(
                                FailCode::InvalidPropertyExpr,
                                None,
                                format!("property expression fails to parse: {e}"),
                            ));
                            return report(failures, ev.path.len().saturating_sub(1));
                        }
                    };
                    match fsm_lang::eval::check_boolean_predicate(sys, &mut e) {
                        Ok(()) => Some(e),
                        Err(e) => {
                            failures.push(fail(
                                FailCode::InvalidPropertyExpr,
                                None,
                                format!("property expression is not a well-typed boolean: {e}"),
                            ));
                            None
                        }
                    }
                }
                None => {
                    failures.push(fail(
                        FailCode::InvalidPropertyExpr,
                        None,
                        format!("{:?} evidence requires a property expression", ev.kind),
                    ));
                    None
                }
            }
        }
        EvidenceKindInput::Deadlock => None,
    };

    if ev.path.is_empty() {
        failures.push(fail(FailCode::EmptyPath, None, "evidence has no states"));
        return report(failures, 0);
    }

    // decode every state
    let mut decoded: Vec<Vec<Value>> = Vec::with_capacity(ev.path.len());
    for (i, step) in ev.path.iter().enumerate() {
        match decode_state(sys, &step.state) {
            Ok(st) => decoded.push(st),
            Err(f) => {
                failures.push(fail(f, Some(i), "state decoding failed"));
                decoded.push(sys.fresh_state()); // placeholder to keep indices
            }
        }
    }

    // length sanity
    let expected_len = ev.path.len() - 1;
    if let Some(declared) = ev.length {
        if declared != expected_len {
            failures.push(fail(
                FailCode::LengthFieldMismatch,
                None,
                format!(
                    "evidence.length = {declared} but path contains {expected_len} transition(s)"
                ),
            ));
        }
    }
    if !failures.is_empty() {
        return report(failures, expected_len);
    }

    // first-step discipline
    if ev.path[0].fired.is_some() {
        failures.push(fail(
            FailCode::FirstStepFired,
            Some(0),
            "the first step is an initial state and must not carry a fired transition",
        ));
    }
    for (i, step) in ev.path.iter().enumerate().skip(1) {
        if step.fired.is_none() {
            failures.push(fail(
                FailCode::LaterStepNotFired,
                Some(i),
                "every step after the first must name the transition fired to reach it",
            ));
        }
    }

    // init
    match fsm_lang::eval::eval(sys, &sys.init, &decoded[0]) {
        Ok(Value::Bool(true)) => {}
        Ok(Value::Bool(false)) => failures.push(fail(
            FailCode::InitialStateNotSelected,
            Some(0),
            "first state is not selected by the init predicate",
        )),
        Err(e) => failures.push(fail(
            FailCode::InitialStateNotSelected,
            Some(0),
            format!("init predicate errored on first state: {e}"),
        )),
        Ok(_) => failures.push(fail(
            FailCode::InitialStateNotSelected,
            Some(0),
            "init predicate did not return a boolean",
        )),
    }

    // replay edges
    for i in 1..ev.path.len() {
        let pre = &decoded[i - 1];
        let post_claimed = &decoded[i];
        let Some(name) = &ev.path[i].fired else {
            continue;
        };
        let Some(t) = sys.transitions.iter().find(|t| &t.name == name) else {
            failures.push(fail(
                FailCode::UnknownTransition,
                Some(i),
                format!("transition '{name}' does not exist"),
            ));
            continue;
        };
        let guard = match sys.guard_holds(t, pre) {
            Ok(g) => g,
            Err(e) => {
                failures.push(fail(
                    FailCode::GuardDisabled,
                    Some(i),
                    format!("guard of '{name}' errored: {e}"),
                ));
                continue;
            }
        };
        if !guard {
            failures.push(fail(
                FailCode::GuardDisabled,
                Some(i),
                format!("guard of '{name}' is false in the pre-state"),
            ));
            continue;
        }
        match sys.apply(t, pre) {
            Ok(post_actual) if &post_actual == post_claimed => {}
            Ok(actual) => failures.push(fail(
                FailCode::UpdateMismatch,
                Some(i),
                format!(
                    "parallel update of '{name}' yields {} but evidence claims {}",
                    render_compact(sys, &actual),
                    render_compact(sys, post_claimed)
                ),
            )),
            Err(e) => failures.push(fail(
                FailCode::UpdateMismatch,
                Some(i),
                format!("update of '{name}' errors: {e}"),
            )),
        }
    }

    // final-state claim
    let last = &decoded[decoded.len() - 1];
    match ev.kind {
        EvidenceKindInput::AgViolation => {
            if let Some(e) = &property_expr {
                match fsm_lang::eval::eval(sys, e, last) {
                    Ok(Value::Bool(false)) => {}
                    Ok(Value::Bool(true)) => failures.push(fail(
                        FailCode::FinalInvariantHolds,
                        Some(ev.path.len() - 1),
                        "final state satisfies the AG invariant, so it is not a counterexample",
                    )),
                    Err(er) => failures.push(fail(
                        FailCode::FinalInvariantHolds,
                        Some(ev.path.len() - 1),
                        format!("invariant errored at final state: {er}"),
                    )),
                    Ok(_) => failures.push(fail(
                        FailCode::FinalInvariantHolds,
                        Some(ev.path.len() - 1),
                        "invariant did not return a boolean",
                    )),
                }
            }
        }
        EvidenceKindInput::EfReachable => {
            if let Some(e) = &property_expr {
                match fsm_lang::eval::eval(sys, e, last) {
                    Ok(Value::Bool(true)) => {}
                    Ok(Value::Bool(false)) => failures.push(fail(
                        FailCode::FinalTargetFalse,
                        Some(ev.path.len() - 1),
                        "final state does not satisfy the EF target",
                    )),
                    Err(er) => failures.push(fail(
                        FailCode::FinalTargetFalse,
                        Some(ev.path.len() - 1),
                        format!("EF target errored at final state: {er}"),
                    )),
                    Ok(_) => failures.push(fail(
                        FailCode::FinalTargetFalse,
                        Some(ev.path.len() - 1),
                        "EF target did not return a boolean",
                    )),
                }
            }
        }
        EvidenceKindInput::Deadlock => {
            match sys.is_terminal(last) {
                Ok(true) => failures.push(fail(
                    FailCode::FinalTerminal,
                    Some(ev.path.len() - 1),
                    "final state satisfies the terminal predicate: a legal stop, not a deadlock",
                )),
                Ok(false) => {}
                Err(e) => failures.push(fail(
                    FailCode::FinalTerminal,
                    Some(ev.path.len() - 1),
                    format!("terminal predicate errored: {e}"),
                )),
            }
            let mut enabled = Vec::new();
            for t in &sys.transitions {
                if sys.guard_holds(t, last).unwrap_or(false) {
                    enabled.push(t.name.clone());
                }
            }
            if !enabled.is_empty() {
                failures.push(fail(
                    FailCode::FinalNotDeadlocked,
                    Some(ev.path.len() - 1),
                    format!("final state still enables transitions {enabled:?}"),
                ));
            }
        }
    }

    report(failures, expected_len)
}

fn report(failures: Vec<Failure>, replayed: usize) -> VerificationReport {
    if failures.is_empty() {
        VerificationReport::ok(replayed)
    } else {
        VerificationReport {
            accepted: false,
            replayed_steps: replayed,
            failures,
        }
    }
}

fn render_compact(sys: &System, st: &[Value]) -> String {
    let parts: Vec<String> = sys
        .vars
        .iter()
        .zip(st)
        .map(|(v, val)| format!("{}={}", v.name, val))
        .collect();
    format!("{{{}}}", parts.join(", "))
}

/// Decode one JSON state map into the typed state vector. Booleans/ints map
/// directly; strings resolve as enum variant names.
fn decode_state(
    sys: &System,
    map: &serde_json::Map<String, serde_json::Value>,
) -> Result<Vec<Value>, FailCode> {
    if map.len() != sys.vars.len() {
        return Err(FailCode::StateMissingVariable);
    }
    let mut st = Vec::with_capacity(sys.vars.len());
    for v in &sys.vars {
        let Some(jv) = map.get(&v.name) else {
            return Err(FailCode::StateMissingVariable);
        };
        let val = match (&v.domain, jv) {
            (Domain::Bool, serde_json::Value::Bool(b)) => Value::Bool(*b),
            (Domain::IntRange { .. }, serde_json::Value::Number(n)) => {
                let Some(i) = n.as_i64() else {
                    return Err(FailCode::StateValueTypeMismatch);
                };
                Value::Int(i)
            }
            (Domain::Enum { variants }, serde_json::Value::String(s)) => {
                let Some(i) = variants.iter().position(|x| x == s) else {
                    return Err(FailCode::StateValueOutOfDomain);
                };
                Value::Int(i as i64)
            }
            _ => return Err(FailCode::StateValueTypeMismatch),
        };
        if !v.domain.contains(&val) {
            return Err(FailCode::StateValueOutOfDomain);
        }
        st.push(val);
    }
    Ok(st)
}
