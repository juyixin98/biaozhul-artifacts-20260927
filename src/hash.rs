//! Deterministic, cross-language hash primitives.
//!
//! The full spec lives in `docs/HASH_SPEC.md` and is mirrored by the
//! independent reference implementation in `scripts/gen_reference.py`.
//! Plain FNV-1a was rejected as the vertex hash: its multiplicative
//! recurrence has weak low-bit avalanche on short, similar keys (e.g.
//! `aa`,`bb`,`cc` all collide modulo small `m`), which produces floods
//! of degenerate hyperedges. Instead each 8-byte chunk is mixed with
//! the splitmix64 finalizer, giving good avalanche at small moduli.

/// Golden-ratio constant used to decorrelate per-vertex hash domains.
pub const GAMMA: u64 = 0x9E37_79B9_7F4A_7C15;
/// Domain separator for fingerprint hashes (distinct from vertex hashes).
pub const FP_DOMAIN: u64 = 0xF9B0_DC5E_3A71_D2C3;
/// Domain separator for the log-masking hash (never persisted).
pub const LOG_DOMAIN: u64 = 0x10C5_A55A_0C5A_55A0;

/// splitmix64 finalizer: a bijective 64-bit mix.
#[inline]
pub fn mix64(mut x: u64) -> u64 {
    x ^= x >> 30;
    x = x.wrapping_mul(0xBF58_476D_1CE4_E5B9);
    x ^= x >> 27;
    x = x.wrapping_mul(0x94D0_49BB_1331_11EB);
    x ^= x >> 31;
    x
}

/// 64-bit hash of `bytes` in a given domain.
///
/// ```text
/// h = mix64(domain ^ (len << 56) ^ 0x... )
/// for each 8-byte little-endian chunk: h = mix64(h ^ mix64(chunk + C))
/// tail bytes are folded in little-endian
/// result   = mix64(h)
/// ```
pub fn keyed64(bytes: &[u8], domain: u64) -> u64 {
    let mut h = mix64(domain ^ ((bytes.len() as u64) << 56) ^ 0xD1B5_4A2E_7E6D_73B3);
    let mut rest = bytes;
    while rest.len() >= 8 {
        let chunk = u64::from_le_bytes(rest[..8].try_into().unwrap());
        h = mix64(h ^ mix64(chunk.wrapping_add(GAMMA)));
        rest = &rest[8..];
    }
    if !rest.is_empty() {
        let mut buf = [0u8; 8];
        buf[..rest.len()].copy_from_slice(rest);
        let tail = u64::from_le_bytes(buf);
        h = mix64(h ^ mix64(tail.wrapping_add(0x9E37_79B9)));
    }
    mix64(h)
}

/// Hash of `key` for vertex position `i` (0, 1 or 2) under `seed`.
#[inline]
pub fn vertex_hash(seed: u64, i: usize, key: &[u8]) -> u64 {
    debug_assert!(i < 3);
    keyed64(key, seed ^ mix64(GAMMA.wrapping_add(i as u64)))
}

/// 64-bit fingerprint of `key` under `seed`. Stored per slot so that
/// out-of-set queries are *verified* instead of blindly trusted.
#[inline]
pub fn fingerprint(seed: u64, key: &[u8]) -> u64 {
    keyed64(key, seed ^ FP_DOMAIN)
}

/// Masked identity of a key for diagnostics: 12 hex chars + length.
/// Contains no key material, so it is safe to log.
pub fn masked_key_id(key: &[u8]) -> String {
    let h = keyed64(key, LOG_DOMAIN);
    format!("fp12={:012x},len={}", h >> 16, key.len())
}
