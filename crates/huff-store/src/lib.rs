//! Filesystem persistence adapter.
//!
//! * [`sha256`]  — self-contained SHA-256 (used for content-addressed names)
//! * [`store`]   — artifact directory + JSON index
//! * [`model`]   — serializable index records
//!
//! All I/O stays under a single configured data directory; no external
//! services are contacted.

pub mod model;
pub mod sha256;
pub mod store;

pub use model::{ArtifactRecord, Index};
pub use store::{Store, StoreError};
