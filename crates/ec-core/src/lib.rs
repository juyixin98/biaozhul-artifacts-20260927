//! Reed-Solomon erasure-coding kernel.
//!
//! Crate layout, one responsibility per module:
//! - [`config`] — fixed/validated coding parameters `(k, m)`.
//! - [`matrix`] — GF(2^8) Cauchy coding matrix and Gaussian-elimination solver.
//! - [`reed_solomon`] — encode / missing-shard rebuild / original recovery.
//! - [`verify`] — per-shard digests, manifest with a covered-field set.
//! - [`error`] — typed failure categories shared across the service.

#![forbid(unsafe_code)]

pub mod config;
pub mod error;
pub mod matrix;
pub mod reed_solomon;
pub mod verify;

pub use config::CodecConfig;
pub use reed_solomon::Shard;
pub use verify::{digest_shard, sha256, verify_shard};
