//! Application service: orchestrates the store, graph builder, solver kernel
//! and evidence verifier behind a small set of typed operations.
//!
//! This is the layer the HTTP handlers call. It deliberately contains no
//! axum/serde-http types, which keeps it usable directly from unit tests and
//! keeps the module contracts explicit:
//!
//! store (state) → graph (reduction) → solver (kernel) → evidence (verification)

use std::collections::BTreeMap;

use serde::Serialize;

use crate::error::{ServiceError, ServiceResult};
use crate::evidence::{verify_assignment, verify_cycle, AssignmentCheck, CycleCheck};
use crate::graph::{ComponentInfo, Graph};
use crate::lang;
use crate::model::Constraint;
use crate::solver::{bellman_ford, SolveOutcome};
use crate::store::{BatchOp, OpResult, Store};

/// Solve answer including the state revision the snapshot was taken at.
#[derive(Debug, Clone, Serialize)]
pub struct SolveAnswer {
    pub revision: u64,
    pub variable_count: usize,
    pub constraint_count: usize,
    pub component_count: usize,
    pub components: Vec<ComponentInfo>,
    pub outcome: SolveOutcome,
}

#[derive(Debug, Clone)]
pub struct ConstraintService {
    store: Store,
}

impl Default for ConstraintService {
    fn default() -> Self {
        Self {
            store: Store::new(),
        }
    }
}

impl ConstraintService {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn revision(&self) -> u64 {
        self.store.read(|s| s.revision())
    }

    pub fn list(&self) -> Vec<Constraint> {
        self.store.read(|s| s.list())
    }

    // ---------- mutations ----------

    pub fn add(&self, c: Constraint) -> ServiceResult<OpResult> {
        self.store.apply_one(BatchOp::Add(c))
    }

    pub fn update(&self, c: Constraint) -> ServiceResult<OpResult> {
        self.store.apply_one(BatchOp::Update(c))
    }

    pub fn delete(&self, id: &str) -> ServiceResult<OpResult> {
        self.store.apply_one(BatchOp::Delete(id.to_string()))
    }

    pub fn clear(&self) -> ServiceResult<OpResult> {
        self.store.apply_one(BatchOp::Clear)
    }

    pub fn batch(&self, ops: Vec<BatchOp>) -> ServiceResult<Vec<OpResult>> {
        self.store.apply_batch(ops)
    }

    /// Parse a text document and add every constraint as a single atomic
    /// batch: a parse failure on line 20 means none of lines 1..19 are added.
    pub fn add_text(&self, text: &str) -> ServiceResult<Vec<OpResult>> {
        let parsed = lang::parse_constraints(text)?;
        let mut ids = std::collections::BTreeSet::new();
        for c in &parsed {
            if !ids.insert(c.id.clone()) {
                return Err(ServiceError::state_conflict(format!(
                    "constraint id '{}' is duplicated within this document",
                    c.id
                )));
            }
        }
        self.store
            .apply_batch(parsed.into_iter().map(BatchOp::Add))
    }

    // ---------- reads / solving ----------

    /// Solve all constraints (or only the named subset). The snapshot is taken
    /// under the store lock; the expensive kernel run happens after releasing
    /// it, so a concurrent batch cannot mutate these constraints mid-solve and
    /// the witness always corresponds to one specific revision.
    pub fn solve(&self, only_ids: Option<&[String]>) -> ServiceResult<SolveAnswer> {
        let (revision, constraints) = self.store.read(|s| {
            let snap = match only_ids {
                Some(ids) => s.get_many(ids)?,
                None => s.snapshot(),
            };
            Ok((s.revision(), snap))
        })?;
        self.solve_snapshot(revision, constraints)
    }

    fn solve_snapshot(
        &self,
        revision: u64,
        constraints: Vec<Constraint>,
    ) -> ServiceResult<SolveAnswer> {
        let graph = Graph::build(&constraints)?;
        let components = graph.component_info();
        let component_count = graph.component_count;
        let variable_count = graph.vertex_count();
        let constraint_count = graph.edges.len();

        let outcome = bellman_ford(&graph)?;

        // Evidence self-check: never serve a witness the independent checker
        // cannot confirm. This is defense in depth around the kernel.
        match &outcome {
            SolveOutcome::Feasible(w) => {
                let assignment: BTreeMap<String, i64> = w.assignment.iter().cloned().collect();
                let check = verify_assignment(&constraints, &assignment)?;
                if !check.satisfied {
                    return Err(ServiceError::computation_failed(format!(
                        "kernel produced an assignment violating {} constraint(s)",
                        check.violations.len()
                    ))
                    .with_detail(serde_json::json!({ "violations": check.violations })));
                }
            }
            SolveOutcome::Infeasible(cyc) => {
                let check = verify_cycle(&constraints, &cyc.cycle_constraint_ids)?;
                if !check.valid {
                    return Err(ServiceError::computation_failed(
                        "kernel produced a cycle that fails independent verification",
                    )
                    .with_detail(serde_json::json!({ "rejection": check.rejection })));
                }
                if check.weight != Some(cyc.weight as i128) {
                    return Err(ServiceError::computation_failed(format!(
                        "kernel cycle weight {} disagrees with independently verified weight {:?}",
                        cyc.weight, check.weight
                    )));
                }
            }
        }

        Ok(SolveAnswer {
            revision,
            variable_count,
            constraint_count,
            component_count,
            components,
            outcome,
        })
    }

    pub fn verify_assignment(
        &self,
        assignment: &BTreeMap<String, i64>,
        only_ids: Option<&[String]>,
    ) -> ServiceResult<AssignmentCheck> {
        let snap = self.store.read(|s| match only_ids {
            Some(ids) => s.get_many(ids),
            None => Ok(s.snapshot()),
        })?;
        verify_assignment(&snap, assignment)
    }

    pub fn verify_cycle(&self, cycle_ids: &[String]) -> ServiceResult<CycleCheck> {
        let snap = self.store.read(|s| s.snapshot());
        verify_cycle(&snap, cycle_ids)
    }
}
