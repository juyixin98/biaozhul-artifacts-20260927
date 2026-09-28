//! Manifest type, canonical covered encoding and JSON persistence form.

use ec_core::error::{EcError, EcResult};
use ec_core::verify::{sha256, DIGEST_ALGORITHM, DIGEST_LEN};
use ec_core::CodecConfig;
use serde::{Deserialize, Serialize};

/// Bumped on any incompatible change to the covered field set or encoding.
pub const FORMAT_VERSION: u8 = 1;
/// Fixed field definition: GF(2^8), modulus 0x11B (AES polynomial),
/// log/exp generator 3. Persisted verbatim so a reader can reject a manifest
/// produced under a different field rather than mis-decoding it.
pub const FIELD_PRIMITIVE: &str = "GF2P8-0x11B-G3";

/// One shard's position-aware digest (`ec_core::verify::digest_shard`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ShardDigestEntry {
    pub index: u16,
    pub digest_hex: String,
}

/// The full, human-readable manifest persisted as one JSON file per object.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Manifest {
    pub format_version: u8,
    pub field_primitive: String,
    pub k: u16,
    pub m: u16,
    pub shard_len: u32,
    pub original_len: u64,
    pub pad_len: u64,
    pub digest_algorithm: String,
    pub object_id: String,
    pub shard_count: u16,
    /// Position-aware shard digests, index order `0..k+m`.
    pub shards: Vec<ShardDigestEntry>,
    /// Digest of the covered encoding — authenticates everything above.
    pub manifest_digest_hex: String,
    /// Informational only (NOT covered itself): names the covered fields.
    pub covered_fields: Vec<String>,
}

/// Names of the covered fields, also the tag order in the TLV encoding.
pub const COVERED_FIELDS: &[&str] = &[
    "format_version",
    "field_primitive",
    "k",
    "m",
    "shard_len",
    "original_len",
    "pad_len",
    "digest_algorithm",
    "object_id",
    "shard_digests",
];

fn push_string(out: &mut Vec<u8>, s: &str) {
    out.extend_from_slice(&(s.len() as u16).to_be_bytes());
    out.extend_from_slice(s.as_bytes());
}

/// Produce the deterministic bytes covered by the manifest digest. This is
/// the format's reference encoding — small enough to verify by hand.
pub fn covered_encoding(m: &Manifest) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&[0u8, m.format_version]);
    out.push(1u8);
    push_string(&mut out, &m.field_primitive);
    out.extend_from_slice(&[2u8, m.k as u8]);
    out.extend_from_slice(&[3u8, m.m as u8]);
    out.extend_from_slice(&[4u8]);
    out.extend_from_slice(&m.shard_len.to_be_bytes());
    out.extend_from_slice(&[5u8]);
    out.extend_from_slice(&m.original_len.to_be_bytes());
    out.extend_from_slice(&[6u8]);
    out.extend_from_slice(&m.pad_len.to_be_bytes());
    out.push(7u8);
    push_string(&mut out, &m.digest_algorithm);
    out.push(8u8);
    push_string(&mut out, &m.object_id);
    out.extend_from_slice(&[9u8]);
    out.extend_from_slice(&(m.shards.len() as u16).to_be_bytes());
    for entry in &m.shards {
        let digest = hex_decode(&entry.digest_hex)
            .expect("covered_encoding called on manifest with non-hex digest");
        out.extend_from_slice(&entry.index.to_be_bytes());
        out.extend_from_slice(&(digest.len() as u32).to_be_bytes());
        out.extend_from_slice(&digest);
    }
    out
}

/// SHA-256 of the canonical covered encoding.
pub fn build_manifest_digest(m: &Manifest) -> Vec<u8> {
    sha256(&covered_encoding(m))
}

impl Manifest {
    /// Construct, fully validate and seal a manifest for a freshly encoded
    /// object. `shard_digests` maps shard index -> [`ec_core::verify::digest_shard`].
    pub fn create(
        object_id: impl Into<String>,
        cfg: &CodecConfig,
        shard_len: usize,
        original_len: u64,
        shard_digests: Vec<(u16, Vec<u8>)>,
    ) -> EcResult<Self> {
        let pad_len = (cfg.k() * shard_len) as u64 - original_len;
        if shard_digests.len() != cfg.n() {
            return Err(EcError::SizeMismatch {
                detail: format!(
                    "need {} shard digests, got {}",
                    cfg.n(),
                    shard_digests.len()
                ),
            });
        }
        let mut entries: Vec<ShardDigestEntry> = shard_digests
            .into_iter()
            .map(|(index, digest)| {
                cfg.check_index(index)?;
                if digest.len() != DIGEST_LEN {
                    return Err(EcError::SizeMismatch {
                        detail: format!("shard {index} digest is {} bytes, expected {DIGEST_LEN}", digest.len()),
                    });
                }
                Ok(ShardDigestEntry {
                    index,
                    digest_hex: hex_encode(&digest),
                })
            })
            .collect::<EcResult<_>>()?;
        entries.sort_by_key(|e| e.index);
        for (pos, e) in entries.iter().enumerate() {
            if e.index as usize != pos {
                return Err(EcError::SizeMismatch {
                    detail: "shard digest entries must cover each index 0..k+m exactly once".into(),
                });
            }
        }

        let mut m = Manifest {
            format_version: FORMAT_VERSION,
            field_primitive: FIELD_PRIMITIVE.to_string(),
            k: cfg.k() as u16,
            m: cfg.m() as u16,
            shard_len: shard_len as u32,
            original_len,
            pad_len,
            digest_algorithm: DIGEST_ALGORITHM.to_string(),
            object_id: object_id.into(),
            shard_count: cfg.n() as u16,
            shards: entries,
            manifest_digest_hex: String::new(),
            covered_fields: COVERED_FIELDS.iter().map(|s| s.to_string()).collect(),
        };
        m.manifest_digest_hex = hex_encode(&build_manifest_digest(&m));
        Ok(m)
    }

    /// Reconstruct the validated coding config.
    pub fn config(&self) -> EcResult<CodecConfig> {
        CodecConfig::new(self.k, self.m)
    }

    /// Structural validation: version/field tags, layout arithmetic, digest
    /// set completeness. Does NOT check the manifest digest (that is
    /// [`Self::verify_digest`]) nor any shard bytes.
    pub fn validate_structure(&self) -> EcResult<CodecConfig> {
        if self.format_version != FORMAT_VERSION {
            return Err(EcError::ManifestFieldMissing(format!(
                "format_version={} unsupported, expected {FORMAT_VERSION}",
                self.format_version
            )));
        }
        if self.field_primitive != FIELD_PRIMITIVE {
            return Err(EcError::ManifestFieldMissing(format!(
                "field_primitive `{}` does not match `{FIELD_PRIMITIVE}`",
                self.field_primitive
            )));
        }
        if self.digest_algorithm != DIGEST_ALGORITHM {
            return Err(EcError::ManifestFieldMissing(format!(
                "digest_algorithm `{}` does not match `{DIGEST_ALGORITHM}`",
                self.digest_algorithm
            )));
        }
        let cfg = self.config()?;
        if self.shard_count as usize != cfg.n() {
            return Err(EcError::SizeMismatch {
                detail: format!(
                    "shard_count={} != k+m={}",
                    self.shard_count,
                    cfg.n()
                ),
            });
        }
        if self.shards.len() != cfg.n() {
            return Err(EcError::SizeMismatch {
                detail: format!("manifest lists {} shard digests, expected {}", self.shards.len(), cfg.n()),
            });
        }
        if self.shard_len == 0 {
            return Err(EcError::SizeMismatch {
                detail: "shard_len must be >= 1".into(),
            });
        }
        let capacity = (cfg.k() * self.shard_len as usize) as u64;
        if self.original_len > capacity {
            return Err(EcError::SizeMismatch {
                detail: format!("original_len={} exceeds capacity {capacity}", self.original_len),
            });
        }
        let expected_pad = capacity - self.original_len;
        if self.pad_len != expected_pad {
            return Err(EcError::ManifestFieldMissing(format!(
                "pad_len={} inconsistent with k*shard_len-original_len={expected_pad}",
                self.pad_len
            )));
        }
        for (pos, e) in self.shards.iter().enumerate() {
            if e.index as usize != pos {
                return Err(EcError::SizeMismatch {
                    detail: format!("shard digest at position {pos} has index {} (must be contiguous from 0)", e.index),
                });
            }
            let digest = hex_decode(&e.digest_hex).map_err(|_| EcError::ManifestFieldMissing(format!(
                "shard {pos} digest is not valid hexadecimal"
            )))?;
            if digest.len() != DIGEST_LEN {
                return Err(EcError::SizeMismatch {
                    detail: format!("shard {pos} digest length {} != {DIGEST_LEN}", digest.len()),
                });
            }
        }
        if self.manifest_digest_hex.len() != DIGEST_LEN * 2
            || hex_decode(&self.manifest_digest_hex).is_err()
        {
            return Err(EcError::ManifestFieldMissing(
                "manifest_digest_hex missing or not 32 bytes of hex".into(),
            ));
        }
        Ok(cfg)
    }

    /// Recalculate the digest over the covered fields and compare.
    pub fn verify_digest(&self) -> bool {
        match hex_decode(&self.manifest_digest_hex) {
            Ok(expected) => expected == build_manifest_digest(self),
            Err(_) => false,
        }
    }

    /// Parse the persisted JSON form and run every structural check, but do
    /// NOT verify the manifest digest — callers then call
    /// [`Self::verify_digest`] so a tampered manifest yields the dedicated
    /// [`EcError::ManifestDigestMismatch`] category rather than a parse error.
    pub fn from_json_str(json: &str) -> EcResult<Self> {
        let manifest: Manifest = serde_json::from_str(json).map_err(|e| {
            EcError::ManifestFieldMissing(format!("manifest JSON parse error: {e}"))
        })?;
        manifest.validate_structure()?;
        Ok(manifest)
    }

    /// Parse + structural validation + manifest digest verification.
    pub fn from_json_verified(json: &str) -> EcResult<Self> {
        let manifest = Self::from_json_str(json)?;
        if !manifest.verify_digest() {
            return Err(EcError::ManifestDigestMismatch);
        }
        Ok(manifest)
    }

    /// Stable JSON form (2-space, field order as declared).
    pub fn to_json_string(&self) -> String {
        serde_json::to_string_pretty(self).expect("Manifest is always JSON-serialisable")
    }

    /// Look up the expected digest bytes for one shard.
    pub fn shard_digest(&self, index: usize) -> Option<Vec<u8>> {
        self.shards
            .get(index)
            .and_then(|e| hex_decode(&e.digest_hex).ok())
    }
}

pub fn hex_encode(bytes: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(H[(b >> 4) as usize] as char);
        s.push(H[(b & 0x0F) as usize] as char);
    }
    s
}

pub fn hex_decode(s: &str) -> Result<Vec<u8>, String> {
    if s.len() % 2 != 0 {
        return Err("odd hex length".into());
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let hi = hex_nibble(bytes[i])?;
        let lo = hex_nibble(bytes[i + 1])?;
        out.push((hi << 4) | lo);
        i += 2;
    }
    Ok(out)
}

fn hex_nibble(c: u8) -> Result<u8, String> {
    match c {
        b'0'..=b'9' => Ok(c - b'0'),
        b'a'..=b'f' => Ok(c - b'a' + 10),
        b'A'..=b'F' => Ok(c - b'A' + 10),
        _ => Err(format!("invalid hex digit {c:#x}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_manifest() -> Manifest {
        let cfg = CodecConfig::new(3, 2).unwrap();
        let digests: Vec<(u16, Vec<u8>)> = (0..5)
            .map(|i| (i as u16, ec_core::verify::digest_shard(i as u16, &[i as u8; 11])))
            .collect();
        Manifest::create("obj-test", &cfg, 11, 31, digests).unwrap()
    }

    #[test]
    fn round_trips_and_verifies() {
        let m = sample_manifest();
        assert!(m.verify_digest());
        let json = m.to_json_string();
        let parsed = Manifest::from_json_verified(&json).unwrap();
        assert_eq!(parsed, m);
        assert_eq!(m.pad_len, 2);
    }

    #[test]
    fn tampering_with_any_covered_field_breaks_digest() {
        let m = sample_manifest();
        let cases = [
            ("original_len", {
                let mut x = m.clone();
                x.original_len += 1;
                x
            }),
            ("pad_len", {
                let mut x = m.clone();
                x.pad_len += 1;
                x
            }),
            ("shard_len", {
                let mut x = m.clone();
                x.shard_len += 1;
                x
            }),
            ("k", {
                let mut x = m.clone();
                x.k = 4;
                x
            }),
            ("one shard digest", {
                let mut x = m.clone();
                let mut d = hex_decode(&x.shards[2].digest_hex).unwrap();
                d[0] ^= 0xFF;
                x.shards[2].digest_hex = hex_encode(&d);
                x
            }),
        ];
        for (name, tampered) in cases {
            assert!(!tampered.verify_digest(), "{name} should break digest");
        }
    }

    #[test]
    fn inconsistent_padding_is_structurally_rejected() {
        let mut m = sample_manifest();
        m.manifest_digest_hex = hex_encode(&build_manifest_digest(&m)); // digest fine
        m.pad_len = 99; // but arithmetic wrong
        let err = m.validate_structure().unwrap_err();
        assert!(matches!(err, EcError::ManifestFieldMissing(_)));
    }
}
