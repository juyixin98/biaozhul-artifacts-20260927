//! Persistence port and filesystem adapter.
//!
//! Split from the kernel on purpose:
//! - everything above (`ec-core`, `ec-format`) has no I/O and is fully
//!   deterministically testable;
//! - this crate owns directory layout, atomic writes and the distinction
//!   between a **missing** shard (an erasure, normal recovery input) and a
//!   **unreadable** shard (an I/O failure that callers must not paper over).
//!
//! On-disk layout per object:
//!
//! ```text
//! <root>/<object_id>/manifest.json
//! <root>/<object_id>/shards/shard-00000.bin … shard-NNNNN.bin
//! ```

#![forbid(unsafe_code)]

pub mod fs_store;
pub mod memory_store;
pub mod paths;

pub use fs_store::FileSystemStore;
pub use memory_store::MemoryStore;
pub use paths::{manifest_path, shard_path, shards_dir};

use std::path::Path;

use ec_core::error::EcError;
use ec_format::Manifest;

/// Read outcome for one shard: the two "not here" states are kept apart.
#[derive(Debug)]
pub enum ShardRead {
    /// File absent on disk — treated as an erasure.
    Missing,
    /// File present and read; its *integrity* is decided by digest
    /// verification, never by this layer.
    Present(Vec<u8>),
}

/// Persistence port. Implementations must be safe to use from multiple
/// concurrent requests for distinct object ids.
pub trait ObjectStore: Send + Sync {
    /// Persist the sealed manifest and all shard bytes for a new object.
    /// Implementations should be all-or-nothing per object.
    fn put_object(
        &self,
        manifest: &Manifest,
        shards: &[Vec<u8>],
    ) -> Result<(), EcError>;

    /// Load + verify an object's manifest; a missing object yields an
    /// [`EcError::Store`] carrying `NOT_FOUND`, a tampered manifest yields
    /// [`EcError::ManifestDigestMismatch`].
    fn get_manifest(&self, object_id: &str) -> Result<Manifest, EcError>;

    /// Read one shard file. Missing files are [`ShardRead::Missing`] (not an
    /// error); actual I/O failures are [`EcError::Store`].
    fn read_shard(&self, object_id: &str, index: u16) -> Result<ShardRead, EcError>;

    /// Persist rebuilt shard bytes (repair write). `index` must be within the
    /// manifest's layout.
    fn write_shard(&self, object_id: &str, index: u16, bytes: &[u8]) -> Result<(), EcError>;

    /// List object ids directly under the root (best-effort, for diagnostics).
    fn list_objects(&self) -> Result<Vec<String>, EcError>;

    /// Absolute root location, for explainable logs/responses.
    fn root_display(&self) -> String;
}

pub(crate) fn ensure_safe_id(id: &str) -> Result<(), EcError> {
    if id.is_empty()
        || id.len() > 128
        || id.contains('/')
        || id.contains('\\')
        || id.contains("..")
        || id.starts_with('.')
    {
        return Err(EcError::Store(format!("unsafe object id: {id:?}")));
    }
    Ok(())
}

pub(crate) fn io_err(context: &str, path: &Path, e: std::io::Error) -> EcError {
    EcError::Store(format!("{context} {}: {e}", path.display()))
}
