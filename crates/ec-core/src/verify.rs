//! Digest primitives.
//!
//! Hash choice is fixed and named (SHA-256) so manifests stay verifiable
//! across versions. Two *domain-separated* digests exist; mixing the two
//! domains is impossible by construction:
//!
//! - shard digest: covers the shard's **position** (so swapping two valid
//!   shards is detected, not just byte corruption) and its bytes;
//! - manifest digest: computed in the `ec-format` crate over the manifest's
//! declared covered-field bytes (length, padding, parameters, all shard
//! digests, …), via [`sha256`].
//!
//! Encoding is explicit length-prefixed concatenation — no ambiguous
//! serialisation format, no trailing-data ambiguity.

use sha2::{Digest, Sha256};

/// Human/algorithm tag persisted in manifests.
pub const DIGEST_ALGORITHM: &str = "SHA-256";
/// Length of every digest in bytes.
pub const DIGEST_LEN: usize = 32;

/// Raw SHA-256 helper (also used by the manifest digest in `ec-format`).
pub fn sha256(input: &[u8]) -> Vec<u8> {
    let mut h = Sha256::new();
    h.update(input);
    h.finalize().to_vec()
}

/// Digest of one shard:
///
/// ```text
/// SHA-256("ec-shard-v1" || u16_be(index) || u64_be(len) || bytes)
/// ```
///
/// Including `index` makes a shard-swap (two individually-valid shards placed
/// at wrong positions) fail verification; including `len` catches truncation
/// even when the manifest shard_len is somehow ignored.
pub fn digest_shard(index: u16, bytes: &[u8]) -> Vec<u8> {
    let mut h = Sha256::new();
    h.update(b"ec-shard-v1");
    h.update(index.to_be_bytes());
    h.update((bytes.len() as u64).to_be_bytes());
    h.update(bytes);
    h.finalize().to_vec()
}

/// Compare an expected digest against actual bytes for a shard.
pub fn verify_shard(index: u16, bytes: &[u8], expected: &[u8]) -> bool {
    expected.len() == DIGEST_LEN
        && constant_time_eq(&digest_shard(index, bytes), expected)
}

/// Constant-time byte comparison so integrity checks do not leak a timing
/// signal about *where* a digest diverged.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn position_and_length_are_part_of_digest() {
        // Same bytes at another position must not verify.
        let d0 = digest_shard(0, b"hello");
        let d1 = digest_shard(1, b"hello");
        assert_ne!(d0, d1);
        assert!(verify_shard(0, b"hello", &d0));
        assert!(!verify_shard(1, b"hello", &d0));
        // Any single-bit change breaks the digest.
        let mut corrupted = b"hello".to_vec();
        corrupted[0] ^= 1;
        assert!(!verify_shard(0, &corrupted, &d0));
    }

    #[test]
    fn known_empty_shard_digest_is_stable() {
        // Regression anchor: the construction string is part of the format,
        // so its output must never silently change.
        let d = digest_shard(0, b"");
        assert_eq!(
            hex::encode(&d),
            hex::encode(sha256(&{
                let mut v = b"ec-shard-v1".to_vec();
                v.extend_from_slice(&0u16.to_be_bytes());
                v.extend_from_slice(&0u64.to_be_bytes());
                v
            }))
        );
        assert_eq!(d.len(), DIGEST_LEN);
    }
}
