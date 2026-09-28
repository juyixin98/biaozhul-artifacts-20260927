//! Orchestration layer: request -> compile -> solve -> verify -> response.
//!
//! This is the single place the HTTP layer and the CLI share. It owns the run
//! logger, feeds it the key intermediate states, and attaches the independent
//! evidence verification to every `not_included` verdict.

use serde::Serialize;

use crate::compiler;
use crate::diagnostics::{self, RunLogger};
use crate::error::EngineResult;
use crate::input::CheckRequest;
use crate::model::Pair;
use crate::solver::{self, CheckOutcome};
use crate::verifier;

#[derive(Debug, Clone, Serialize)]
pub struct CheckResponse {
    pub run_id: String,
    pub verdict: solver::Verdict,
    pub silent_action: String,
    pub aligned_alphabet: Vec<String>,
    pub models: ModelSummary,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub counterexample: Option<CounterexampleView>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unknown: Option<solver::UnknownReason>,
    pub stats: solver::Stats,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verification: Option<verifier::Verification>,
    pub log_file: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModelSummary {
    pub specification: SideSummary,
    pub implementation: SideSummary,
}

#[derive(Debug, Clone, Serialize)]
pub struct SideSummary {
    pub name: String,
    pub states: usize,
    pub transitions: usize,
    pub accepting_states: usize,
    pub initial: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct CounterexampleView {
    pub trace: Vec<String>,
    pub implementation_replay: crate::witness::Replay,
    pub spec_macro_path: Vec<Vec<String>>,
    pub impl_macro_path: Vec<Vec<String>>,
    /// Human-readable justification of why this is a failure.
    pub reason: String,
}

fn side_summary(pair: &Pair, spec: bool) -> SideSummary {
    let lts = if spec { &pair.spec } else { &pair.impl_ };
    SideSummary {
        name: lts.name.clone(),
        states: lts.state_count(),
        transitions: lts.edge_count(),
        accepting_states: lts.accepting.iter().filter(|a| **a).count(),
        initial: lts.name_of_state(lts.initial).to_string(),
    }
}

/// Compile + check + verify with a fresh run id.
pub fn run_check(request: &CheckRequest) -> EngineResult<CheckResponse> {
    run_check_with_id(request, diagnostics::new_run_id())
}

pub fn run_check_with_id(request: &CheckRequest, run_id: String) -> EngineResult<CheckResponse> {
    let mut log = RunLogger::new(run_id.clone());
    log.event(
        "request_received",
        serde_json::json!({
            "spec": request.specification.name,
            "impl": request.implementation.name,
            "silent": request.silent_action,
            "alphabet_explicit": request.alphabet.is_some(),
        }),
    );

    let pair = compiler::compile(request)?;
    log.event(
        "compiled",
        serde_json::json!({
            "aligned_alphabet": pair.label_names,
            "spec_states": pair.spec.state_count(),
            "impl_states": pair.impl_.state_count(),
            "spec_transitions": pair.spec.edge_count(),
            "impl_transitions": pair.impl_.edge_count(),
        }),
    );

    let limits = request.limits();
    let outcome: CheckOutcome = solver::check(&pair, &limits)?;
    log.event(
        "solver_done",
        serde_json::json!({
            "verdict": outcome.verdict,
            "stats": outcome.stats,
            "unknown": outcome.unknown,
        }),
    );

    let mut verification = None;
    let mut counterexample = None;
    if let Some(ce) = &outcome.counterexample {
        // Independent cross-check before we report the counterexample.
        let v = verifier::verify_counterexample(&pair, &ce.implementation_replay, &ce.trace)?;
        log.event(
            "counterexample_verified",
            serde_json::json!({
                "trace": ce.trace,
                "confirmed": v.confirmed,
                "problems": v.problems,
                "impl_edge_count": ce.implementation_replay.edge_count,
                "spec_macro_path": ce.spec_path,
                "impl_macro_path": ce.impl_path,
            }),
        );
        let reason = format!(
            "implementation accepts observable trace {:?} (ending in accepting state '{}'), \
             but after the same trace the specification can only be in non-accepting states {:?}",
            ce.trace,
            ce.implementation_replay.accepting_state,
            ce.spec_path.last().cloned().unwrap_or_default()
        );
        counterexample = Some(CounterexampleView {
            trace: ce.trace.clone(),
            implementation_replay: ce.implementation_replay.clone(),
            spec_macro_path: ce.spec_path.clone(),
            impl_macro_path: ce.impl_path.clone(),
            reason,
        });
        verification = Some(v);
    }

    Ok(CheckResponse {
        run_id,
        verdict: outcome.verdict,
        silent_action: pair.silent_name.clone(),
        aligned_alphabet: pair.label_names.clone(),
        models: ModelSummary {
            specification: side_summary(&pair, true),
            implementation: side_summary(&pair, false),
        },
        counterexample,
        unknown: outcome.unknown,
        stats: outcome.stats,
        verification,
        log_file: log.log_path().map(|p| p.display().to_string()),
    })
}
