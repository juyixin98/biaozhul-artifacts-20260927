//! On-disk data format: the object manifest and its authenticated digest.
//!
//! Everything needed to interpret shards — original length, padding, coding
//! parameters, algorithm version, and the per-shard SHA-256 list — lives in
//! one JSON manifest. The manifest carries a `manifest_digest` computed over a
//! canonical serialization of *all other fields*, so tampering with the
//! original length, a shard checksum, k/m, or the version marker is detectable.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Format/algorithm version marker. Bumped on any incompatible change to the
/// manifest layout or the coding construction.
pub const FORMAT_VERSION: u32 = 1;
/// Name of the digest field that authenticates the rest of the manifest.
pub const DIGEST_FIELD: &str = "manifest_digest";

/// Fixed algorithm block embedded in every manifest.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AlgorithmSpec {
    /// Always "reed-solomon-vandermonde-systematic" for this version.
    pub scheme: String,
    /// Field definition marker, e.g. `GF256-PP0x11D-G2/v1`.
    pub field: String,
    /// Manifest/format version.
    pub format_version: u32,
}

impl AlgorithmSpec {
    pub fn current() -> Self {
        Self {
            scheme: "reed-solomon-vandermonde-systematic".to_string(),
            field: crate::gf256::GF_VERSION.to_string(),
            format_version: FORMAT_VERSION,
        }
    }
}

/// One authenticated shard record.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ShardRecord {
    /// Shard index in `0..k+m`; data shards are `0..k`, parity `k..k+m`.
    pub index: u8,
    /// `data` or `parity` — role implied by index, stored for readability.
    pub role: String,
    /// File name relative to the object directory.
    pub file: String,
    /// Size of the shard in bytes (all shards share this length).
    pub size: u64,
    /// Lowercase hex SHA-256 of the shard bytes.
    pub sha256: String,
}

/// The object manifest.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Manifest {
    /// Object identifier (directory name).
    pub object_id: String,
    pub algorithm: AlgorithmSpec,
    /// Number of data shards.
    pub k: u8,
    /// Number of parity shards.
    pub m: u8,
    /// Original, unpadded payload size in bytes.
    pub original_len: u64,
    /// Size of each shard; `k * shard_len >= original_len`, the tail is zero
    /// padding. `pad_len = k*shard_len - original_len` is implicit but also
    /// explicitly authenticated below.
    pub shard_len: u64,
    /// Number of appended zero bytes (`k*shard_len - original_len`).
    pub pad_len: u64,
    /// Creation timestamp, seconds since UNIX epoch (informational only).
    pub created_at_unix: u64,
    /// SHA-256 of the original (unpadded) payload.
    pub payload_sha256: String,
    /// One record per shard, sorted by index; its *length and entries* are
    /// covered by the manifest digest.
    pub shards: Vec<ShardRecord>,
    /// Hex SHA-256 over the canonical JSON of every field above (this field
    /// itself removed). Present on disk; absent while computing the digest.
    #[serde(default)]
    pub manifest_digest: String,
}

/// Compute SHA-256 over the canonical JSON representation of all manifest
/// fields except `manifest_digest`.
///
/// Canonical form: a JSON object parsed into a `serde_json::Value` (BTreeMap
/// ordering => keys sorted ascending, no whitespace, no platform dependence),
/// with the `manifest_digest` key deleted. This is independent of field order
/// on disk and can be reproduced by any language's JSON parser.
pub fn canonical_digest(m: &Manifest) -> String {
    let mut value = serde_json::to_value(m).expect("manifest serializes");
    let obj = value
        .as_object_mut()
        .expect("manifest is a JSON object");
    obj.remove(DIGEST_FIELD);
    let canonical = serde_json::to_vec(&value).expect("canonical value serializes");
    let hash = Sha256::digest(&canonical);
    hex::encode(hash)
}

/// Fill `manifest_digest` from the other fields.
pub fn seal(m: &mut Manifest) {
    m.manifest_digest.clear();
    m.manifest_digest = canonical_digest(m);
}

/// Manifest validation failures — distinct categories, surfaced verbatim.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ManifestError {
    /// File cannot be parsed as JSON of this schema.
    Parse(String),
    /// `manifest_digest` does not match the canonical digest of the fields.
    DigestMismatch { expected: String, computed: String },
    /// Empty digest field.
    MissingDigest,
    /// Unsupported format version or scheme/field markers.
    UnsupportedVersion {
        supported: u32,
        found: u32,
    },
    UnsupportedAlgorithm {
        found: String,
    },
    /// Structural inconsistency between fields.
    Inconsistent(String),
}

impl ManifestError {
    pub fn code(&self) -> &'static str {
        match self {
            ManifestError::Parse(_) => "MANIFEST_PARSE_ERROR",
            ManifestError::DigestMismatch { .. } => "MANIFEST_DIGEST_MISMATCH",
            ManifestError::MissingDigest => "MANIFEST_DIGEST_MISSING",
            ManifestError::UnsupportedVersion { .. } => "UNSUPPORTED_VERSION",
            ManifestError::UnsupportedAlgorithm { .. } => "UNSUPPORTED_ALGORITHM",
            ManifestError::Inconsistent(_) => "MANIFEST_INCONSISTENT",
        }
    }
}

impl std::fmt::Display for ManifestError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ManifestError::Parse(e) => write!(f, "manifest parse error: {e}"),
            ManifestError::DigestMismatch { expected, computed } => write!(
                f,
                "manifest digest mismatch: stored={expected}, computed={computed}"
            ),
            ManifestError::MissingDigest => write!(f, "manifest has no manifest_digest"),
            ManifestError::UnsupportedVersion { supported, found } => write!(
                f,
                "unsupported format version: found {found}, supported {supported}"
            ),
            ManifestError::UnsupportedAlgorithm { found } => {
                write!(f, "unsupported algorithm marker: {found}")
            }
            ManifestError::Inconsistent(r) => write!(f, "manifest inconsistent: {r}"),
        }
    }
}

impl std::error::Error for ManifestError {}

/// Parse and fully verify a manifest byte stream: schema, digest, version
/// markers, and internal consistency. Returns the verified manifest.
pub fn parse_and_verify(bytes: &[u8]) -> Result<Manifest, ManifestError> {
    let m: Manifest =
        serde_json::from_slice(bytes).map_err(|e| ManifestError::Parse(e.to_string()))?;

    if m.manifest_digest.is_empty() {
        return Err(ManifestError::MissingDigest);
    }
    let computed = canonical_digest(&m);
    if computed != m.manifest_digest {
        return Err(ManifestError::DigestMismatch {
            expected: m.manifest_digest.clone(),
            computed,
        });
    }

    let cur = AlgorithmSpec::current();
    if m.algorithm.format_version != FORMAT_VERSION {
        return Err(ManifestError::UnsupportedVersion {
            supported: FORMAT_VERSION,
            found: m.algorithm.format_version,
        });
    }
    if m.algorithm.scheme != cur.scheme || m.algorithm.field != cur.field {
        return Err(ManifestError::UnsupportedAlgorithm {
            found: format!("{} / {}", m.algorithm.scheme, m.algorithm.field),
        });
    }

    // Internal consistency: lengths and shard listing.
    if m.k == 0 || m.m == 0 {
        return Err(ManifestError::Inconsistent("k and m must be >= 1".into()));
    }
    if m.shards.len() != (m.k as usize) + (m.m as usize) {
        return Err(ManifestError::Inconsistent(format!(
            "shard list has {} entries, expected {}",
            m.shards.len(),
            m.k as usize + m.m as usize
        )));
    }
    for (pos, rec) in m.shards.iter().enumerate() {
        if rec.index as usize != pos {
            return Err(ManifestError::Inconsistent(format!(
                "shard list not sorted by index at position {pos}: got {}",
                rec.index
            )));
        }
        let expected_role = if pos < m.k as usize { "data" } else { "parity" };
        if rec.role != expected_role {
            return Err(ManifestError::Inconsistent(format!(
                "shard {} role is {}, expected {expected_role}",
                rec.index, rec.role
            )));
        }
        if rec.size != m.shard_len {
            return Err(ManifestError::Inconsistent(format!(
                "shard {} size {} != shard_len {}",
                rec.index, rec.size, m.shard_len
            )));
        }
        if rec.sha256.len() != 64 || !rec.sha256.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(ManifestError::Inconsistent(format!(
                "shard {} sha256 is not 64 hex chars",
                rec.index
            )));
        }
    }
    let padded = (m.k as u64) * m.shard_len;
    if padded != m.original_len + m.pad_len {
        return Err(ManifestError::Inconsistent(format!(
            "k*shard_len={padded} != original_len({})+pad_len({})",
            m.original_len, m.pad_len
        )));
    }
    if m.pad_len >= m.k as u64 && m.original_len > 0 {
        // Padding must be strictly less than one shard for minimal layout.
        return Err(ManifestError::Inconsistent(format!(
            "pad_len {} is not minimal (>= k {})",
            m.pad_len, m.k
        )));
    }
    Ok(m)
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::Digest;

    fn sample() -> Manifest {
        let mut m = Manifest {
            object_id: "obj-test".into(),
            algorithm: AlgorithmSpec::current(),
            k: 2,
            m: 1,
            original_len: 3,
            shard_len: 2,
            pad_len: 1,
            created_at_unix: 1_700_000_000,
            payload_sha256: hex::encode(Sha256::digest(b"abc")),
            shards: vec![
                ShardRecord {
                    index: 0,
                    role: "data".into(),
                    file: "shard-000.bin".into(),
                    size: 2,
                    sha256: "00".repeat(32),
                },
                ShardRecord {
                    index: 1,
                    role: "data".into(),
                    file: "shard-001.bin".into(),
                    size: 2,
                    sha256: "11".repeat(32),
                },
                ShardRecord {
                    index: 2,
                    role: "parity".into(),
                    file: "shard-002.bin".into(),
                    size: 2,
                    sha256: "22".repeat(32),
                },
            ],
            manifest_digest: String::new(),
        };
        seal(&mut m);
        m
    }

    #[test]
    fn seal_and_verify_roundtrip() {
        let m = sample();
        let bytes = serde_json::to_vec(&m).unwrap();
        let back = parse_and_verify(&bytes).unwrap();
        assert_eq!(back, m);
    }

    #[test]
    fn tampering_with_original_len_is_detected() {
        let m = sample();
        let mut value: serde_json::Value = serde_json::to_value(&m).unwrap();
        value["original_len"] = 4.into(); // forge length, keep old digest
        let bytes = serde_json::to_vec(&value).unwrap();
        let err = parse_and_verify(&bytes).unwrap_err();
        assert_eq!(err.code(), "MANIFEST_DIGEST_MISMATCH");
    }

    #[test]
    fn tampering_with_shard_list_is_detected() {
        let m = sample();
        let mut value: serde_json::Value = serde_json::to_value(&m).unwrap();
        // Flip one shard checksum (silent bad-shard forgery).
        value["shards"][0]["sha256"] = "aa".repeat(32).into();
        let bytes = serde_json::to_vec(&value).unwrap();
        assert_eq!(
            parse_and_verify(&bytes).unwrap_err().code(),
            "MANIFEST_DIGEST_MISMATCH"
        );
        // Delete a shard entry -> digest mismatch first (list length covered).
        let mut value2: serde_json::Value = serde_json::to_value(&m).unwrap();
        value2["shards"].as_array_mut().unwrap().pop();
        let bytes2 = serde_json::to_vec(&value2).unwrap();
        assert_eq!(
            parse_and_verify(&bytes2).unwrap_err().code(),
            "MANIFEST_DIGEST_MISMATCH"
        );
    }

    #[test]
    fn version_and_reseal_attacks() {
        let m = sample();
        let mut value: serde_json::Value = serde_json::to_value(&m).unwrap();
        value["algorithm"]["format_version"] = 99.into();
        // An attacker who reseals with the changed version:
        let resealed = value.to_string().replace(
            &format!("\"manifest_digest\":\"{}\"", m.manifest_digest),
            "\"manifest_digest\":\"x\"",
        );
        // Reseal properly via our canonical routine on the altered struct.
        let mut tampered: Manifest = serde_json::from_value(value).unwrap();
        seal(&mut tampered);
        let bytes = serde_json::to_vec(&tampered).unwrap();
        assert_eq!(
            parse_and_verify(&bytes).unwrap_err().code(),
            "UNSUPPORTED_VERSION"
        );
        assert_eq!(
            parse_and_verify(resealed.as_bytes()).unwrap_err().code(),
            "MANIFEST_DIGEST_MISMATCH"
        );
    }

    #[test]
    fn padding_consistency_is_checked() {
        let mut m = sample();
        m.pad_len = 2; // 2*2 != 3+2
        seal(&mut m);
        let bytes = serde_json::to_vec(&m).unwrap();
        assert_eq!(
            parse_and_verify(&bytes).unwrap_err().code(),
            "MANIFEST_INCONSISTENT"
        );
    }
}
