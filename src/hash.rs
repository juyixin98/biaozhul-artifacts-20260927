//! Deterministic keyed hashing.
//!
//! This is **not** cryptographic. It is a self-contained, endian-independent
//! 64-bit mix (SplitMix64-style finalizer + FNV-1a style byte fold) whose
//! exact integer semantics are mirrored byte-for-byte by the independent
//! Python reference under `tests/reference/`. Because all arithmetic is
//! defined mod 2^64, outputs are identical on every platform.
//!
//! Contract (reproduced in `tests/reference/mphf_ref.py`):
//!
//! ```text
//! mix64(z):
//!   z = (z + 0x9e3779b97f4a7c15) & mask
//!   z = ((z ^ (z >> 30)) * 0xbf58476d1ce4e5b9) & mask
//!   z = ((z ^ (z >> 27)) * 0x94d049bb133111eb) & mask
//!   z ^ (z >> 31)
//!
//! stream(key, seed, salt):
//!   h = 0xcbf29ce484222325
//!   for b in key:                       # bytes in order
//!       h ^= b
//!       h = (h * 0x00000100000001B3) & mask
//!   h ^= seed;  h = mix64(h)
//!   h ^= salt;  h = mix64(h)
//!
//! edge(key, seed):
//!   h0 = stream(key, seed, 0x...d1)
//!   h1 = stream(key, seed, 0x...d2)
//!   vertices = [h0 mod m, h1 mod m, (h0+h1) mod m]   (must be distinct)
//!
//! (h2 = stream(key, seed, 0x...d3) is reserved for future variants.)
//! ```
//!
//! Each key derives two *independent* 64-bit streams `h0, h1` (distinct
//! salts). Its hyperedge is the textbook BDZ triple
//! `{h0 mod m, h1 mod m, (h0 + h1) mod m}`. If the three residues are not
//! distinct (probability O(1/m) per key for prime `m >= 3`), the attempt
//! fails and the builder retries with another seed. Prime `m` is chosen by
//! [`crate::builder::BuildConfig::vertex_count`].

#[inline]
pub fn mix64(z: u64) -> u64 {
    let mut z = z.wrapping_add(0x9e37_79b9_7f4a_7c15);
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    z ^ (z >> 31)
}

/// One independent hash stream for `key` under `seed` with stream `salt`.
#[inline]
pub fn stream(key: &[u8], seed: u64, salt: u64) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in key {
        h ^= b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h ^= seed;
    let h = mix64(h);
    let h = h ^ salt;
    mix64(h)
}

const SALT_0: u64 = 0xa5a5_5a5a_d1d1_d1d1;
const SALT_1: u64 = 0xa5a5_5a5a_d2d2_d2d2;
const SALT_2: u64 = 0xa5a5_5a5a_d3d3_d3d3;
/// Salt used for the member fingerprint stream (independent of edge streams).
pub const SALT_FP: u64 = 0x5252_5252_f0f0_f0f1;

/// Three raw stream hashes for a key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EdgeHash {
    pub h0: u64,
    pub h1: u64,
    pub h2: u64,
}

pub fn edge_hash(key: &[u8], seed: u64) -> EdgeHash {
    EdgeHash {
        h0: stream(key, seed, SALT_0),
        h1: stream(key, seed, SALT_1),
        h2: stream(key, seed, SALT_2),
    }
}

/// Select the hyperedge for `key`: `{h0, h1, h0+h1} mod m` (BDZ). Returns
/// `None` when the three residues are not distinct (builder retries seeds).
pub fn edge(key: &[u8], seed: u64, m: u64) -> Option<[u64; 3]> {
    let EdgeHash { h0, h1, .. } = edge_hash(key, seed);
    let a = h0 % m;
    let b = h1 % m;
    let c = h0.wrapping_add(h1) % m;
    if a != b && a != c && b != c {
        Some([a, b, c])
    } else {
        None
    }
}

/// Fingerprint of `key`, in `1..=2^bits` (0 reserved as "no entry").
/// `bits` must be one of 8, 16, 32, 64.
pub fn fingerprint(key: &[u8], seed: u64, bits: u8) -> u64 {
    debug_assert!(matches!(bits, 8 | 16 | 32 | 64));
    let raw = stream(key, seed, SALT_FP);
    let modulus = 1u128 << bits;
    (raw as u128 % (modulus - 1)) as u64 + 1
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprint_never_zero_and_in_range() {
        for bits in [8u8, 16, 32] {
            for seed in [0u64, 1, 0xdead_beef] {
                for k in [b"".as_slice(), b"a", b"alpha", b"\x00\x01\x02"] {
                    let fp = fingerprint(k, seed, bits);
                    assert!(fp >= 1 && fp <= (1u64 << bits) - 1);
                }
            }
        }
    }

    #[test]
    fn edge_is_distinct_and_in_range() {
        // Collisions across all three independent residues are possible but
        // rare at real sizes; m=1009 makes the test deterministic.
        for m in [3u64, 5, 7, 101, 1009] {
            for seed in [0u64, 42, 0xdead_beef] {
                if let Some(e) = edge(b"some-key", seed, m) {
                    assert!(e.iter().all(|&v| v < m));
                    assert_ne!(e[0], e[1]);
                    assert_ne!(e[0], e[2]);
                    assert_ne!(e[1], e[2]);
                }
            }
        }
    }

    #[test]
    fn edge_collision_is_signalled_not_silently_duplicated() {
        // m=3: collision means None, never an edge with repeated vertices.
        let mut none_seen = false;
        for seed in 0..1000u64 {
            match edge(b"k", seed, 3) {
                Some(e) => {
                    assert_ne!(e[0], e[1]);
                    assert_ne!(e[0], e[2]);
                    assert_ne!(e[1], e[2]);
                }
                None => none_seen = true,
            }
        }
        assert!(none_seen, "expected some residue collisions at m=3");
    }
}
