//! Kernel-level error types.
//!
//! These are *semantic* failures (invalid references, cross-manager use,
//! unknown variables). Lexing/parsing failures live in [`crate::lang`].

use std::fmt;

/// A stable machine-readable failure category, surfaced verbatim by the API.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorKind {
    /// Variable name is not declared in the manager's order.
    UnknownVariable,
    /// The declared variable order itself is invalid (e.g. duplicate name).
    InvalidOrder,
    /// A [`crate::kernel::NodeRef`] was issued by a different manager.
    ForeignManager,
    /// A reference predates the garbage collection that recycled its node.
    StaleReference,
    /// A packed edge references a node slot that never existed.
    InvalidNode,
    /// The variable mapping is not a bijection over its domain/range.
    NonBijectiveMapping,
    /// Some appearing variable has no image in the mapping.
    UnmappedVariable,
    /// Mapped variables are ordered differently on the two sides.
    OrderMismatch,
    /// Exhaustive verification was asked to enumerate too many assignments.
    VerificationLimit,
}

impl ErrorKind {
    /// Short kebab-case code used in API payloads.
    pub fn as_code(self) -> &'static str {
        match self {
            ErrorKind::UnknownVariable => "unknown-variable",
            ErrorKind::InvalidOrder => "invalid-order",
            ErrorKind::ForeignManager => "foreign-manager",
            ErrorKind::StaleReference => "stale-reference",
            ErrorKind::InvalidNode => "invalid-node",
            ErrorKind::NonBijectiveMapping => "non-bijective-mapping",
            ErrorKind::UnmappedVariable => "unmapped-variable",
            ErrorKind::OrderMismatch => "order-mismatch",
            ErrorKind::VerificationLimit => "verification-limit",
        }
    }
}

/// Kernel error carrying a category and a human-readable context message.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KernelError {
    pub kind: ErrorKind,
    pub detail: String,
}

impl KernelError {
    pub(crate) fn new(kind: ErrorKind, detail: impl Into<String>) -> Self {
        KernelError {
            kind,
            detail: detail.into(),
        }
    }
}

impl fmt::Display for KernelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.kind.as_code(), self.detail)
    }
}

impl std::error::Error for KernelError {}

pub(crate) type Result<T> = std::result::Result<T, KernelError>;
