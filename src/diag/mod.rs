//! Diagnostics: structured, redaction-aware records of what the service decided.
//!
//! Every decision record carries the request/job id, the constraint id under
//! consideration and the key solver state it depended on. Clause *contents* are only
//! rendered when the clause is not marked sensitive: otherwise the record shows the
//! clause id plus the formula fingerprint instead of literals.

use std::collections::HashSet;

use serde::{Deserialize, Serialize};

use crate::language::{ClauseId, Formula};
use crate::solver::SolveStatus;

/// Safe, content-free way to refer to a clause in a diagnostic.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClauseRef {
    pub id: ClauseId,
    /// "redacted" for sensitive clauses; "plain" otherwise.
    pub content: ClauseContentRef,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case")]
pub enum ClauseContentRef {
    Plain {
        literals: Vec<i64>,
    },
    Redacted {
        /// Stable fingerprint of the whole input, allowing correlation across logs
        /// without revealing literals.
        formula_fingerprint: String,
    },
}

#[derive(Debug, Clone)]
pub struct Redactor {
    sensitive: HashSet<String>,
    fingerprint: String,
}

impl Redactor {
    pub fn new(formula: &Formula) -> Self {
        let sensitive = formula
            .clauses
            .iter()
            .filter(|c| c.sensitive)
            .map(|c| c.id.0.clone())
            .collect();
        Self {
            sensitive,
            fingerprint: format!("{:016x}", formula.fingerprint()),
        }
    }

    pub fn fingerprint(&self) -> &str {
        &self.fingerprint
    }

    /// Build a safe reference to the clause at `position`.
    pub fn clause_ref(&self, formula: &Formula, position: usize) -> ClauseRef {
        let clause = &formula.clauses[position];
        let content = if clause.sensitive || self.sensitive.contains(&clause.id.0) {
            ClauseContentRef::Redacted {
                formula_fingerprint: self.fingerprint.clone(),
            }
        } else {
            ClauseContentRef::Plain {
                literals: clause.literals.iter().map(|l| l.dimacs()).collect(),
            }
        };
        ClauseRef {
            id: clause.id.clone(),
            content,
        }
    }
}

/// One deletion decision in the extraction run — the auditable "why kept/removed".
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DecisionRecord {
    pub seq: usize,
    pub request_id: String,
    pub candidate: ClauseRef,
    /// UNSAT => the contradiction survives its removal, i.e. the clause is redundant
    /// within the current subset and is DELETED. SAT => removing it destroys the
    /// contradiction, so it is KEPT. UNKNOWN => no claim, conservatively KEPT.
    pub trial_status: SolveStatus,
    pub action: DecisionAction,
    pub basis: String,
    pub solver_calls_used: usize,
    pub decisions_spent: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub solver_reason: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DecisionAction {
    /// Trial SAT: clause is needed for the contradiction, retained.
    Kept,
    /// Trial UNSAT: clause redundant in the current subset, removed.
    Removed,
    /// Trial UNKNOWN or budget: cannot decide, conservatively retained.
    KeptUndecided,
}

/// Why an extraction run stopped. Mirrors the core outcome but lives in diag so the
/// API layer can serialize a shared vocabulary without depending on core internals.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StopReason {
    Completed,
    CompletedWithUnknownTrials,
    BudgetExhausted,
    Cancelled,
    InputSat,
    InputUnknown,
    InvalidInput,
}
