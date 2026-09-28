//! Wire response DTOs and mapping from solver results.
//!
//! Every claimed witness and deadlock produced by the solver is independently
//! re-verified here with `pn-verify` before serialization, so a solver result
//! is never reported with an unchecked proof attached.

use serde::Serialize;

use pn_core::Net;
use pn_solver::{AnalysisOutcome, InvariantCheck, TargetOutcome, WitnessFire};
use pn_verify::{verify_deadlock, verify_witness, StepClaim};

#[derive(Debug, Serialize)]
pub struct ApiResponse {
    pub status: &'static str,
    pub run_id: String,
    pub server_version: String,
    pub schema: &'static str,
    pub result: Option<AnalysisDto>,
    pub error: Option<ErrorDto>,
}

#[derive(Debug, Serialize)]
pub struct ErrorDto {
    pub category: String,
    pub message: String,
    pub details: Vec<ErrorDetailDto>,
}

#[derive(Debug, Serialize)]
pub struct ErrorDetailDto {
    pub category: String,
    pub message: String,
    pub pointer: String,
}

#[derive(Debug, Serialize)]
pub struct AnalysisDto {
    pub net_name: String,
    pub places: Vec<String>,
    pub transitions: Vec<String>,
    pub initial: Vec<u64>,
    pub capacity_box_size: Option<u128>,
    pub targets: Vec<TargetDto>,
    pub deadlocks: Vec<DeadlockDto>,
    pub deadlocks_truncated: bool,
    pub invariants: Vec<InvariantDto>,
    pub invariant_generation_truncated: bool,
    pub total_states_expanded: usize,
    pub scope: ScopeDto,
}

#[derive(Debug, Serialize)]
pub struct ScopeDto {
    pub model: String,
    pub equivalent_to_unbounded_reachability_decision: bool,
    pub explanation: String,
}

#[derive(Debug, Serialize)]
pub struct TargetDto {
    pub label: Option<String>,
    pub target: Vec<u64>,
    pub verdict: String,
    pub reachable: bool,
    pub stop_reason: String,
    pub distance: Option<usize>,
    pub states_expanded: usize,
    pub states_discovered: usize,
    pub path: Option<Vec<WitnessDto>>,
    pub witness_verification: Option<WitnessVerificationDto>,
    pub invariant_checks: Vec<InvariantCheckDto>,
}

#[derive(Debug, Serialize)]
pub struct WitnessDto {
    pub step: usize,
    pub transition: String,
    pub before: Vec<u64>,
    pub after: Vec<u64>,
}

#[derive(Debug, Serialize)]
pub struct WitnessVerificationDto {
    pub valid: bool,
    pub endpoint: Vec<u64>,
    pub failures: Vec<VerificationFailureDto>,
}

#[derive(Debug, Serialize)]
pub struct VerificationFailureDto {
    pub kind: String,
    pub step: usize,
    pub detail: String,
    pub fire_failure: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct InvariantCheckDto {
    pub weights: Vec<i128>,
    pub support: Vec<usize>,
    pub support_minimal_in_set: bool,
    pub weighted_sum_initial: i128,
    pub weighted_sum_target: i128,
    /// Unequal weighted sums are a sound unreachability certificate.
    pub necessary_condition_holds: bool,
}

#[derive(Debug, Serialize)]
pub struct DeadlockDto {
    pub marking: Vec<u64>,
    pub distance: usize,
    pub independent_verification: DeadlockVerificationDto,
}

#[derive(Debug, Serialize)]
pub struct DeadlockVerificationDto {
    pub is_deadlock: bool,
    pub enabled_transitions: Vec<String>,
    pub within_capacity: bool,
    pub reasons: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct InvariantDto {
    pub weights: Vec<i128>,
    pub support: Vec<usize>,
    pub support_minimal_in_set: bool,
}

pub fn success_dto(
    outcome: &AnalysisOutcome,
    net: &Net,
    run_id: &str,
    server_version: &str,
) -> ApiResponse {
    ApiResponse {
        status: "ok",
        run_id: run_id.to_string(),
        server_version: server_version.to_string(),
        schema: "petri-analysis/v1",
        result: Some(analysis_dto(outcome, net)),
        error: None,
    }
}

pub fn error_dto(
    run_id: &str,
    server_version: &str,
    category: &str,
    message: String,
    details: Vec<ErrorDetailDto>,
) -> ApiResponse {
    ApiResponse {
        status: "error",
        run_id: run_id.to_string(),
        server_version: server_version.to_string(),
        schema: "petri-analysis/v1",
        result: None,
        error: Some(ErrorDto {
            category: category.to_string(),
            message,
            details,
        }),
    }
}

fn analysis_dto(outcome: &AnalysisOutcome, net: &Net) -> AnalysisDto {
    let targets = outcome
        .targets
        .iter()
        .enumerate()
        .map(|(i, t)| {
            let checks = outcome.invariant_checks.get(i).cloned().unwrap_or_default();
            target_dto(net, t, checks)
        })
        .collect();

    let deadlocks = outcome
        .deadlocks
        .iter()
        .map(|d| {
            let v = verify_deadlock(net, &d.marking);
            DeadlockDto {
                marking: d.marking.clone(),
                distance: d.distance,
                independent_verification: DeadlockVerificationDto {
                    is_deadlock: v.is_deadlock,
                    enabled_transitions: v.enabled_transitions,
                    within_capacity: v.within_capacity,
                    reasons: v.reasons,
                },
            }
        })
        .collect();

    AnalysisDto {
        net_name: outcome.net_name.clone(),
        places: outcome.place_names.clone(),
        transitions: outcome.transition_names.clone(),
        initial: outcome.initial.clone(),
        capacity_box_size: outcome.capacity_box_size,
        targets,
        deadlocks,
        deadlocks_truncated: outcome.deadlocks_truncated,
        invariants: outcome
            .invariants
            .iter()
            .map(|i| InvariantDto {
                weights: i.weights.clone(),
                support: i.support.clone(),
                support_minimal_in_set: i.support_minimal_in_set,
            })
            .collect(),
        invariant_generation_truncated: outcome.invariant_generation_truncated,
        total_states_expanded: outcome.total_states_expanded,
        scope: ScopeDto {
            model: outcome.scope.model.clone(),
            equivalent_to_unbounded_reachability_decision: false,
            explanation: outcome.scope.explanation.clone(),
        },
    }
}

fn target_dto(net: &Net, t: &TargetOutcome, checks: Vec<InvariantCheck>) -> TargetDto {
    let (path, witness_verification) = match &t.path {
        None => (None, None),
        Some(steps) => {
            // Independently replay the solver's witness, endpoint included.
            let claims: Vec<StepClaim> = steps
                .iter()
                .map(|w| StepClaim {
                    transition: w.transition.clone(),
                    expected_after: Some(w.after.clone()),
                })
                .collect();
            let verdict = verify_witness(net, &claims, Some(&t.target));
            let dto = WitnessVerificationDto {
                valid: verdict.valid,
                endpoint: verdict.endpoint,
                failures: verdict
                    .failures
                    .into_iter()
                    .map(|f| VerificationFailureDto {
                        kind: f.kind.as_str().to_string(),
                        step: f.step,
                        detail: f.detail,
                        fire_failure: f.fire_failure.map(|s| s.to_string()),
                    })
                    .collect(),
            };
            (
                Some(steps.iter().map(witness_dto).collect()),
                Some(dto),
            )
        }
    };

    TargetDto {
        label: t.label.clone(),
        target: t.target.clone(),
        verdict: t.verdict.clone(),
        reachable: t.reachable,
        stop_reason: t.stop_reason.clone(),
        distance: t.distance,
        states_expanded: t.states_expanded,
        states_discovered: t.states_discovered,
        path,
        witness_verification,
        invariant_checks: checks.into_iter().map(check_dto).collect(),
    }
}

fn witness_dto(w: &WitnessFire) -> WitnessDto {
    WitnessDto {
        step: w.step,
        transition: w.transition.clone(),
        before: w.before.clone(),
        after: w.after.clone(),
    }
}

fn check_dto(c: InvariantCheck) -> InvariantCheckDto {
    InvariantCheckDto {
        weights: c.weights,
        support: c.support,
        support_minimal_in_set: c.support_minimal_in_set,
        weighted_sum_initial: c.at_initial,
        weighted_sum_target: c.at_target,
        necessary_condition_holds: c.preserves_target,
    }
}
