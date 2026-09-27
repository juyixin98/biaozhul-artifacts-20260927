//! Counterexample replay verification.
//!
//! Every counterexample reported by the SMT kernel is replayed through the
//! independent concrete interpreter. A report is only considered trustworthy
//! when the replay reaches the same failing node id with the same failure
//! category. Any disagreement is surfaced as an internal inconsistency, never
//! silently turned into success.

use serde::Serialize;

use crate::evidence::concrete::{run, ConcreteInput, ConcreteStep, FailureKind, FailureSite};
use crate::lang::Program;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ReplayStatus {
    /// The concrete replay reproduced the claimed failure at the same node.
    Reproduced,
    /// The replay did not fail at all.
    NoFailure,
    /// The replay failed, but at a different node.
    DifferentNode { claimed_node: u32, replay_node: u32 },
    /// The replay failed at the same node but with a different failure kind.
    DifferentKind,
}

#[derive(Debug, Clone, Serialize)]
pub struct ReplayCheck {
    pub status: ReplayStatus,
    pub steps: Vec<ConcreteStep>,
    pub replay_failure: Option<FailureSite>,
}

impl ReplayCheck {
    pub fn reproduced(&self) -> bool {
        matches!(self.status, ReplayStatus::Reproduced)
    }
}

/// Replay one claimed failure against the concrete interpreter.
pub fn replay_counterexample(
    program: &Program,
    kind: FailureKind,
    node_id: u32,
    input: &ConcreteInput,
) -> ReplayCheck {
    let result = match run(program, input) {
        Ok(r) => r,
        // Inputs produced from the model are width-validated; a malformed
        // input can never reproduce anything.
        Err(_) => {
            return ReplayCheck {
                status: ReplayStatus::NoFailure,
                steps: Vec::new(),
                replay_failure: None,
            }
        }
    };
    let Some(fail) = result.failure.clone() else {
        return ReplayCheck {
            status: ReplayStatus::NoFailure,
            steps: result.steps,
            replay_failure: None,
        };
    };
    let status = if fail.node_id != node_id {
        ReplayStatus::DifferentNode {
            claimed_node: node_id,
            replay_node: fail.node_id,
        }
    } else if fail.kind != kind {
        ReplayStatus::DifferentKind
    } else {
        ReplayStatus::Reproduced
    };
    ReplayCheck {
        status,
        steps: result.steps,
        replay_failure: Some(fail),
    }
}
