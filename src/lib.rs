//! Minimal perfect hash over immutable key sets.
//!
//! Module responsibilities:
//! - [`hash`]: deterministic keyed hash streams / hyperedge selection.
//! - [`kernel`]: graph peeling + `g` assignment core (edge-source agnostic).
//! - [`index`]: in-memory index, rank support, membership probes.
//! - [`format`]: versioned, checksummed binary persistence.
//! - [`builder`]: dedup, seeded retries with a cap, verifier material.
//! - [`persistence`]: filesystem adapter (sets directory, atomic writes).
//! - [`config`]: build/server configuration types.
//! - [`server`]: HTTP verification interface (Axum).

pub mod builder;
pub mod config;
pub mod error;
pub mod format;
pub mod hash;
pub mod index;
pub mod kernel;
pub mod persistence;
pub mod server;

pub use builder::{build, BuildConfig, BuildReport};
pub use error::{ErrorKind, MphfError, Result};
pub use index::{MphfIndex, Probe, RejectReason, VerifyMode};
pub use persistence::Store;
