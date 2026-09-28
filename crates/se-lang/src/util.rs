//! Small cross-crate utilities (content identifiers; no external hashing dependency).

/// FNV-1a 64-bit hash rendered as hex. Used to give a program a stable content id for
/// log correlation without pulling in another crate.
pub fn fnv1a64(bytes: &[u8]) -> u64 {
    const OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut h = OFFSET;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(PRIME);
    }
    h
}

/// Short human-facing content id: `p-<12 hex chars>`.
pub fn content_id(canonical_json: &str) -> String {
    format!("p-{:012x}", fnv1a64(canonical_json.as_bytes()) & 0x000f_ffff_ffff_ffff)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fnv_stable_and_distinct() {
        assert_eq!(fnv1a64(b""), 0xcbf2_9ce4_8422_2325);
        assert_ne!(content_id("a"), content_id("b"));
        assert_eq!(content_id("abc"), content_id("abc"));
    }
}
