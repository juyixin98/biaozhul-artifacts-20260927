//! 证据层：证据类型契约与独立检查器。

pub mod checker;
pub mod types;

pub use checker::{
    check_model, check_outcome, check_proof, independent_canonicalize,
    ModelError, OutcomeCheckError, ProofError,
};
pub use types::{
    ClauseRef, Model, NormalizeReport, Outcome, ProofStep, ResolutionProof,
};
