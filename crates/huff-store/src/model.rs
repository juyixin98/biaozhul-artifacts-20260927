//! On-disk index records.

use serde::{Deserialize, Serialize};

/// One stored artifact entry in `index.json`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ArtifactRecord {
    /// Content-addressed id (hex SHA-256 of the *container* bytes).
    pub id: String,
    /// File name relative to the data directory.
    pub path: String,
    /// Container byte length.
    pub container_len: u64,
    /// Original (decoded) byte length.
    pub original_len: u64,
    pub block_count: u32,
    pub format_version: u8,
    /// Creation timestamp as Unix seconds (synthetic/local, no NTP needed).
    pub created_unix: u64,
    /// Source label supplied by the caller, if any.
    pub label: Option<String>,
}

/// Full index document.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct Index {
    pub schema: u32,
    pub artifacts: Vec<ArtifactRecord>,
}
