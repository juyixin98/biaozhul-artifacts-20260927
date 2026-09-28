//! Errors raised by the indexing kernel.

use core::fmt;

/// Construction / invariant violations.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CoreError {
    /// A `from_sorted_unique*` constructor received a slice that was not
    /// strictly increasing.
    NotSortedUnique,
    /// A trusted constructor's claimed cardinality did not match the data.
    CardinalityMismatch {
        /// Cardinality claimed by the caller.
        claimed: usize,
        /// Cardinality actually present.
        actual: usize,
    },
    /// A trusted constructor received a value outside `[0, 2^16)`.
    OutOfBounds,
}

impl fmt::Display for CoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CoreError::NotSortedUnique => {
                f.write_str("input is not strictly increasing (sorted, unique)")
            }
            CoreError::CardinalityMismatch { claimed, actual } => write!(
                f,
                "cardinality mismatch: claimed {claimed}, actual {actual}"
            ),
            CoreError::OutOfBounds => f.write_str("value out of bounds for a chunk"),
        }
    }
}

impl std::error::Error for CoreError {}
