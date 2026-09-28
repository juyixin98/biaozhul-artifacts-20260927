//! Explainable result/verification reports.
//!
//! Every endpoint returns the same shape: request identity, processing
//! context, an explicit outcome, per-shard classification, and — critically —
//! separate lists for *failures* and *uncertain conclusions*. Logs reuse the
//! same vocabulary, so a log line and the JSON response can be matched field
//! for field.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

/// Per-shard classification. Missing and digest-bad are distinct on purpose:
/// only the latter proves corruption happened.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ShardStatus {
    /// Present and digest-verified.
    Good,
    /// File absent: an erasure of unknown cause.
    Missing,
    /// Present but its digest did not match — corruption proven. Treated as
    /// an erasure for recovery purposes.
    BadDigest,
    /// Could not be read for a reason other than absence (I/O error). This
    /// is *uncertain*: we neither trust nor erase-declare the shard.
    ReadError,
    /// Rebuilt during this request from the coding equations.
    Rebuilt,
}

impl ShardStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            ShardStatus::Good => "good",
            ShardStatus::Missing => "missing",
            ShardStatus::BadDigest => "bad_digest",
            ShardStatus::ReadError => "read_error",
            ShardStatus::Rebuilt => "rebuilt",
        }
    }
}

/// Detailed verification result for one object.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VerifyReport {
    pub object_id: String,
    /// Where data was read from (store root or "stateless request").
    pub location: String,
    pub format_version: u8,
    pub field_primitive: String,
    pub digest_algorithm: String,
    pub k: u16,
    pub m: u16,
    pub shard_len: u32,
    pub original_len: u64,
    pub pad_len: u64,
    /// true iff manifest digest recomputation matched.
    pub manifest_digest_ok: bool,
    /// index -> status, indices present in ascending order.
    pub shards: BTreeMap<u16, ShardStatus>,
    pub good_count: usize,
    pub missing_indices: Vec<u16>,
    pub bad_digest_indices: Vec<u16>,
    pub read_error_indices: Vec<u16>,
    /// Whether the original is mathematically recoverable from good shards.
    pub recoverable: bool,
    /// Machine-readable failure category if no recovery is possible/safe.
    pub failure_code: Option<String>,
    /// Conclusions that are plausible but could not be proven — always a
    /// separate list, never merged into hard failures.
    pub uncertainties: Vec<String>,
    /// Ordered list of the key steps performed.
    pub steps: Vec<String>,
}

impl VerifyReport {
    pub fn new(object_id: String, location: String) -> Self {
        Self {
            object_id,
            location,
            format_version: 0,
            field_primitive: String::new(),
            digest_algorithm: String::new(),
            k: 0,
            m: 0,
            shard_len: 0,
            original_len: 0,
            pad_len: 0,
            manifest_digest_ok: false,
            shards: BTreeMap::new(),
            good_count: 0,
            missing_indices: Vec::new(),
            bad_digest_indices: Vec::new(),
            read_error_indices: Vec::new(),
            recoverable: false,
            failure_code: None,
            uncertainties: Vec::new(),
            steps: Vec::new(),
        }
    }

    pub fn step(&mut self, s: impl Into<String>) {
        self.steps.push(s.into());
    }
}

/// Result of an encode request.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EncodeReport {
    pub request_id: String,
    pub object_id: String,
    pub location: String,
    pub k: u16,
    pub m: u16,
    pub n: u16,
    pub shard_len: u32,
    pub original_len: u64,
    pub pad_len: u64,
    pub field_primitive: String,
    pub manifest_digest_hex: String,
    pub steps: Vec<String>,
}

/// Result of a decode/recover request.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DecodeReport {
    pub request_id: String,
    pub object_id: String,
    pub location: String,
    pub recovered: bool,
    /// Base64 original bytes — present only when `recovered` and the caller
    /// requested data (`include_data`, default true).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data_b64: Option<String>,
    pub original_len: u64,
    pub verify: VerifyReport,
    pub used_shard_indices: Vec<u16>,
    pub rebuilt_shard_indices: Vec<u16>,
    pub failure_code: Option<String>,
    pub failures: Vec<String>,
    pub uncertainties: Vec<String>,
    pub steps: Vec<String>,
}

/// Result of a repair request.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RepairReport {
    pub request_id: String,
    pub object_id: String,
    pub location: String,
    pub repaired: bool,
    pub targets: Vec<u16>,
    /// index -> base64 rebuilt bytes (also persisted when store-backed).
    pub rebuilt_shards_b64: BTreeMap<u16, String>,
    pub verify: VerifyReport,
    pub used_shard_indices: Vec<u16>,
    pub failure_code: Option<String>,
    pub failures: Vec<String>,
    pub uncertainties: Vec<String>,
    pub steps: Vec<String>,
}
