//! Typed, machine-readable failure categories.
//!
//! The HTTP layer maps these to status codes one-to-one, so callers never
//! have to parse prose. [`Error::uncertain`] marks conclusions that are
//! *plausible* but not proven (e.g. a hash mismatch without independent
//! evidence) — the API surfaces these separately from hard failures.

use std::fmt;

/// Result alias used kernel-wide.
pub type EcResult<T> = Result<T, EcError>;

/// Kernel failure categories.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum EcError {
    /// `(k, m)` parameters are invalid (k < 1, m < 1 or k + m > 255).
    InvalidConfig(String),
    /// Shard index out of `0..k+m`.
    InvalidShardIndex { index: u16, total: u16 },
    /// The same shard index appeared more than once in one request.
    DuplicateShardIndex(u16),
    /// Input data / shard sizes do not match the manifest's declared layout.
    SizeMismatch { detail: String },
    /// Digest comparison failed for the given shard; this is a *bad* shard,
    /// not a missing one, and must be treated as an erasure.
    DigestMismatch { index: u16 },
    /// A field expected inside the manifest's covered/checksummed set was
    /// absent or could not be parsed.
    ManifestFieldMissing(String),
    /// The manifest's own digest does not match its covered fields
    /// (tampering or an incompatible writer version).
    ManifestDigestMismatch,
    /// Fewer than k usable shards → by design we refuse to guess.
    /// Carries how many shards were available/required.
    InsufficientShards { available: usize, required: usize },
    /// Requested reconstruction set cannot be solved (should not happen for
    /// a Cauchy matrix with distinct rows; kept as an explicit guard).
    NotReconstructable(String),
    /// Persistence-layer failure.
    Store(String),
    /// Anything else, with a categorised label.
    Internal(String),
}

impl EcError {
    /// Stable machine-readable code returned by the API.
    pub fn code(&self) -> &'static str {
        match self {
            EcError::InvalidConfig(_) => "INVALID_CONFIG",
            EcError::InvalidShardIndex { .. } => "INVALID_SHARD_INDEX",
            EcError::DuplicateShardIndex(_) => "DUPLICATE_SHARD_INDEX",
            EcError::SizeMismatch { .. } => "SIZE_MISMATCH",
            EcError::DigestMismatch { .. } => "DIGEST_MISMATCH",
            EcError::ManifestFieldMissing(_) => "MANIFEST_FIELD_MISSING",
            EcError::ManifestDigestMismatch => "MANIFEST_DIGEST_MISMATCH",
            EcError::InsufficientShards { .. } => "INSUFFICIENT_SHARDS",
            EcError::NotReconstructable(_) => "NOT_RECONSTRUCTABLE",
            EcError::Store(_) => "STORE_ERROR",
            EcError::Internal(_) => "INTERNAL",
        }
    }
}

impl fmt::Display for EcError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EcError::InvalidConfig(s) => write!(f, "invalid coding config: {s}"),
            EcError::InvalidShardIndex { index, total } => {
                write!(f, "shard index {index} out of range 0..{total}")
            }
            EcError::DuplicateShardIndex(i) => write!(f, "duplicate shard index {i} in request"),
            EcError::SizeMismatch { detail } => write!(f, "size mismatch: {detail}"),
            EcError::DigestMismatch { index } => {
                write!(f, "shard {index} failed integrity digest (bad, not missing)")
            }
            EcError::ManifestFieldMissing(name) => {
                write!(f, "manifest field `{name}` is missing or malformed")
            }
            EcError::ManifestDigestMismatch => {
                write!(f, "manifest digest does not match its covered fields")
            }
            EcError::InsufficientShards {
                available,
                required,
            } => write!(
                f,
                "only {available} usable shard(s), {required} required; no fabricated data is returned"
            ),
            EcError::NotReconstructable(s) => write!(f, "not reconstructable: {s}"),
            EcError::Store(s) => write!(f, "store error: {s}"),
            EcError::Internal(s) => write!(f, "internal error: {s}"),
        }
    }
}

impl std::error::Error for EcError {}
