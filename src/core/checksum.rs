//! Checksum primitives, implemented locally with no external crates.
//!
//! * [`crc32`] — IEEE CRC-32 (reflected, poly 0xEDB88320), the zlib/DEFLATE
//!   polynomial, used for block payload integrity.
//! * [`fnv1a64`] — FNV-1a 64-bit, used for *dictionary chaining* digests.
//!   It is not a cryptographic hash; it only needs to detect a wrong
//!   predecessor cheaply and deterministically.

/// IEEE CRC-32 of `bytes`, with conventional xor-out.
#[must_use]
pub fn crc32(bytes: &[u8]) -> u32 {
    crc32_update(0xFFFF_FFFF, bytes) ^ 0xFFFF_FFFF
}

/// Continue a reflected CRC-32 computation (accumulator *without* xor-out).
pub fn crc32_update(mut crc: u32, bytes: &[u8]) -> u32 {
    for &b in bytes {
        crc ^= u32::from(b);
        // Eight rounds per byte; deliberately table-free so the kernel stays
        // tiny and obvious under review.
        for _ in 0..8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (0xEDB8_8320 & mask);
        }
    }
    crc
}

const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

/// FNV-1a 64-bit over a sequence of fragments.
#[must_use]
pub fn fnv1a64(fragments: &[&[u8]]) -> u64 {
    let mut h = FNV_OFFSET;
    for frag in fragments {
        for &b in *frag {
            h ^= u64::from(b);
            h = h.wrapping_mul(FNV_PRIME);
        }
    }
    h
}

/// Dictionary digest: domain-separated FNV-1a over `(index, dict bytes)`.
///
/// The `b"LZDICT:1\n"` prefix and the length framing make it impossible for a
/// raw payload to accidentally collide with a genuine dictionary digest, and
/// bind the digest to the block index it is expected *after*.
#[must_use]
pub fn dict_digest(index_after: u32, dict: &[u8]) -> u64 {
    fnv1a64(&[
        b"LZDICT:1\n",
        &index_after.to_le_bytes(),
        &(dict.len() as u64).to_le_bytes(),
        dict,
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc32_known_vectors() {
        // RFC 1952 / zlib reference values.
        assert_eq!(crc32(b""), 0x0000_0000);
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(crc32(b"a"), 0xE8B7_BE43);
    }

    #[test]
    fn crc32_is_streamable() {
        let whole = crc32(b"split me in two");
        let acc = crc32_update(0xFFFF_FFFF, b"split me ");
        let split = crc32_update(acc, b"in two") ^ 0xFFFF_FFFF;
        assert_eq!(whole, split);
    }

    #[test]
    fn fnv_known_vector_and_digest_framing() {
        // Official FNV-1a 64 vector for "".
        assert_eq!(fnv1a64(&[b""]), FNV_OFFSET);
        // Official vector for "a" is 0xaf63dc4c8601ec8c.
        assert_eq!(fnv1a64(&[b"a"]), 0xaf63_dc4c_8601_ec8c);
        // Raw FNV is a streaming hash: fragment boundaries are invisible by
        // design — the length framing lives in `dict_digest`.
        assert_eq!(fnv1a64(&[b"ab", b""]), fnv1a64(&[b"a", b"b"]));
        // The framed digest, though, binds index and length.
        assert_ne!(dict_digest(0, b"x"), dict_digest(1, b"x"));
        assert_ne!(dict_digest(0, b"x"), dict_digest(0, b"y"));
        assert_ne!(dict_digest(0, b"ab"), dict_digest(0, b"a"));
    }
}
