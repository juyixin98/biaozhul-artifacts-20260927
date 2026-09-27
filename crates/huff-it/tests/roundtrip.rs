//! Property-style roundtrip coverage generated locally (not from the Python
//! fixtures): the full byte alphabet, extreme skews and deterministic random
//! data. Both the production and independent decoders must reproduce the input.

use huff_core::container::{decode_container, encode_container};
use huff_it::indie;

/// Tiny deterministic PRNG (xorshift64*) — reproducible without external data.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn byte(&mut self) -> u8 {
        (self.next() & 0xFF) as u8
    }
}

fn roundtrip_everywhere(data: &[u8], block_size: u32, tag: &str) {
    let blob = encode_container(data, block_size)
        .unwrap_or_else(|e| panic!("[{tag}] encode failed: {e:?}"));
    let prod = decode_container(&blob)
        .unwrap_or_else(|e| panic!("[{tag}] production decode failed: {e:?}"));
    assert_eq!(prod, data, "[{tag}] production mismatch");
    let other = indie::decode(&blob)
        .unwrap_or_else(|e| panic!("[{tag}] independent decode failed: {e:?}"));
    assert_eq!(other, data, "[{tag}] independent mismatch");
}

#[test]
fn every_byte_value_roundtrips() {
    let data: Vec<u8> = (0u16..256).map(|b| b as u8).collect();
    roundtrip_everywhere(&data, 65536, "all-bytes-once");
    let mut repeated = Vec::new();
    for _ in 0..20 {
        repeated.extend_from_slice(&data);
    }
    roundtrip_everywhere(&repeated, 257, "all-bytes-blocked"); // odd block size
}

#[test]
fn extreme_skews_roundtrip() {
    for (heavy, others) in [(0u8, 10u8), (255, 0), (127, 200)] {
        let mut data = vec![heavy; 50_000];
        for i in 0..300 {
            data.push(if i % 2 == 0 { others } else { heavy.wrapping_add(1) });
        }
        roundtrip_everywhere(&data, 4096, "skew");
    }
    // Degenerate: the same byte only (one-symbol convention at scale).
    roundtrip_everywhere(&vec![b'z'; 100_000], 4096, "one-symbol-100k");
}

#[test]
fn deterministic_random_roundtrip() {
    for (seed, len, block) in [
        (0xA5A5_1234_5678_9ABCu64, 1, 65536),
        (0xDEAD_BEEF_CAFE_BABEu64, 1024, 128),
        (0x0F0F_00FF_F0F0_FFFFu64, 50_000, 4096),
        (1u64, 200_000, 65536),
    ] {
        let mut r = Rng(seed);
        let data: Vec<u8> = (0..len).map(|_| r.byte()).collect();
        roundtrip_everywhere(&data, block, &format!("random-{seed:x}"));
    }
}

#[test]
fn empty_and_single_byte_have_defined_shapes() {
    let empty = encode_container(b"", 65536).unwrap();
    assert_eq!(decode_container(&empty).unwrap(), b"");
    assert_eq!(indie::decode(&empty).unwrap(), b"");

    for byte in [0u8, 1, 254, 255] {
        let blob = encode_container(&[byte], 65536).unwrap();
        assert_eq!(decode_container(&blob).unwrap(), &[byte]);
        assert_eq!(indie::decode(&blob).unwrap(), &[byte]);
    }
}

#[test]
fn multi_block_lengths_are_reconstructed_exactly() {
    // Lengths around every block boundary must not lose or duplicate bytes.
    for &len in &[4095usize, 4096, 4097, 8191, 8192, 8193, 12_289] {
        let mut rng = Rng(len as u64 | 1);
        let data: Vec<u8> = (0..len).map(|_| rng.byte()).collect();
        roundtrip_everywhere(&data, 4096, &format!("boundary-{len}"));
    }
}
