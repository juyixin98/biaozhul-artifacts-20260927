//! Wire-level data transfer objects.
//!
//! DTOs are separate from domain types so that serde attributes (rename,
//! flattening, optional text blocks) never leak into the kernel. Conversion
//! goes through [`crate::model::Constraint::new`], which re-validates.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::error::ServiceResult;
use crate::model::Constraint;
use crate::service::SolveAnswer;
use crate::solver::{FeasibleWitness, NegativeCycle};

/// One constraint in JSON form: `{lhs} - {rhs} <= {bound}`.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct ConstraintDto {
    pub id: String,
    pub lhs: String,
    pub rhs: String,
    pub bound: i64,
}

impl ConstraintDto {
    pub fn into_constraint(self) -> ServiceResult<Constraint> {
        Constraint::new(self.id, &self.lhs, &self.rhs, self.bound)
    }
}

#[derive(Debug, Deserialize)]
pub struct AddRequest {
    #[serde(default)]
    pub constraints: Vec<ConstraintDto>,
    /// Optional block in the text DSL (see crate::lang).
    #[serde(default)]
    pub text: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct ReplaceRequest {
    #[serde(default)]
    pub constraints: Vec<ConstraintDto>,
    #[serde(default)]
    pub text: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct UpdateRequest {
    pub constraint: ConstraintDto,
}

#[derive(Debug, Deserialize)]
pub struct DeleteRequest {
    pub id: String,
}

#[derive(Debug, Default, Deserialize)]
pub struct SolveRequest {
    /// Restrict the solve to a subset of stored constraint ids.
    #[serde(default)]
    pub only_ids: Option<Vec<String>>,
}

#[derive(Debug, Deserialize)]
pub struct VerifyRequest {
    #[serde(default)]
    pub assignment: Option<BTreeMap<String, i64>>,
    #[serde(default)]
    pub cycle: Option<Vec<String>>,
    #[serde(default)]
    pub only_ids: Option<Vec<String>>,
}

#[derive(Debug, Serialize)]
pub struct VerifyResponse {
    /// "assignment" or "cycle"
    pub kind: String,
    pub result: serde_json::Value,
}

/// Solve response flattened for convenient clients:
/// `status` + either `witness` (feasible) or `conflict` (infeasible).
#[derive(Debug, Serialize)]
pub struct SolveResponse {
    pub revision: u64,
    pub variable_count: usize,
    pub constraint_count: usize,
    pub component_count: usize,
    pub components: serde_json::Value,
    #[serde(flatten)]
    pub answer: SolveResultDto,
}

#[derive(Debug, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum SolveResultDto {
    Feasible { witness: WitnessDto },
    Infeasible { conflict: ConflictDto },
}

#[derive(Debug, Serialize)]
pub struct WitnessDto {
    pub assignment: BTreeMap<String, i64>,
    pub iterations: usize,
    pub trace: serde_json::Value,
}

#[derive(Debug, Serialize)]
pub struct ConflictDto {
    /// Ordered original constraint ids forming the negative cycle.
    pub cycle_constraint_ids: Vec<String>,
    pub cycle_vertices: Vec<String>,
    pub weight: i64,
    pub iterations: usize,
    pub trace: serde_json::Value,
}

impl SolveResponse {
    pub fn from_answer(a: SolveAnswer) -> Self {
        let answer = match a.outcome {
            crate::solver::SolveOutcome::Feasible(FeasibleWitness {
                assignment,
                iterations,
                trace,
            }) => SolveResultDto::Feasible {
                witness: WitnessDto {
                    assignment: assignment.into_iter().collect(),
                    iterations,
                    trace: serde_json::to_value(trace).expect("trace serializes"),
                },
            },
            crate::solver::SolveOutcome::Infeasible(NegativeCycle {
                cycle_constraint_ids,
                cycle_vertices,
                weight,
                iterations,
                trace,
            }) => SolveResultDto::Infeasible {
                conflict: ConflictDto {
                    cycle_constraint_ids,
                    cycle_vertices,
                    weight,
                    iterations,
                    trace: serde_json::to_value(trace).expect("trace serializes"),
                },
            },
        };
        SolveResponse {
            revision: a.revision,
            variable_count: a.variable_count,
            constraint_count: a.constraint_count,
            component_count: a.component_count,
            components: serde_json::to_value(a.components).expect("components serialize"),
            answer,
        }
    }
}
