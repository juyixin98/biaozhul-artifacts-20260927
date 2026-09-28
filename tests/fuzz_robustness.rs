//! Adversarial robustness: feed random and structure-aware mutated bytes to
//! the container parser and raw decoder. The hard requirement is *no panic*
//! (no index OOB, arithmetic overflow, allocation blow-up) across thousands
//! of inputs; errors must be the documented enum variants.

use rangecode::container::{decode_container, encode_adaptive, Budgets};
use rangecode::range::{decode_vec, RangeDecoder};
use rangecode::table::FreqTable;

/// Deterministic LCG so the fuzz run is reproducible without an RNG crate.
struct Lcg(u64);
impl Lcg {
    fn next_u64(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next_u64() % n as u64) as usize
    }
}

#[test]
fn random_garbage_never_panics_container_parser() {
    let mut rng = Lcg(0xC0FFEE);
    let budgets = Budgets::default();
    for _ in 0..20_000 {
        let len = rng.below(400);
        let mut bytes = vec![0u8; len];
        // Sometimes start with magic so we pass the first branch too.
        if rng.below(3) == 0 && len >= 4 {
            bytes[..4].copy_from_slice(b"RCMP");
        }
        for b in bytes.iter_mut() {
            *b = (rng.next_u64() & 0xFF) as u8;
        }
        let _ = decode_container(&bytes, &budgets);
    }
}

#[test]
fn mutated_valid_containers_never_panic() {
    let msg: Vec<u8> = (0..2_000u32).map(|i| (i % 7) as u8).collect();
    let blob = encode_adaptive(&msg, 256, 2048, 128).unwrap();
    let mut rng = Lcg(0xBADF00D);
    let budgets = Budgets::default();
    for _ in 0..5_000 {
        let mut b = blob.clone();
        // 1-3 random byte flips, inserts, or truncations.
        for _ in 0..1 + rng.below(3) {
            match rng.below(3) {
                0 if !b.is_empty() => {
                    let i = rng.below(b.len());
                    b[i] ^= 1 << rng.below(8);
                }
                1 if b.len() > 1 => {
                    let i = rng.below(b.len());
                    b.truncate(i.max(1));
                }
                _ => {
                    let i = rng.below(b.len() + 1);
                    b.insert(i, (rng.next_u64() & 0xFF) as u8);
                }
            }
        }
        let _ = decode_container(&b, &budgets);
    }
}

#[test]
fn random_five_byte_streams_never_panic_raw_decoder() {
    let mut rng = Lcg(0x5EED);
    let table = FreqTable::new(&[1, 2, 3, 4, 5], 256).unwrap();
    for _ in 0..50_000 {
        let mut payload = vec![0u8; 5 + rng.below(30)];
        for b in payload.iter_mut() {
            *b = (rng.next_u64() & 0xFF) as u8;
        }
        payload[0] = 0; // satisfy seed so we probe deeper
        if let Ok(mut dec) = RangeDecoder::new(&payload) {
            for _ in 0..20 {
                match dec.decode(&table) {
                    Ok(s) => assert!(s < 5),
                    Err(_) => break,
                }
            }
        }
    }
}

#[test]
fn oversized_declared_lengths_cannot_forge_allocations() {
    // A tiny valid header whose declared counts are huge must be rejected by
    // budgets before a symbol-sized allocation occurs.
    let msg: Vec<u8> = (0..100u32).map(|i| (i % 4) as u8).collect();
    let mut blob = encode_adaptive(&msg, 8, 1024, 0).unwrap();
    // Rewrite declared symbol count (offset 16..24) to u64::MAX/2 and fix
    // header CRC.
    blob[16..24].copy_from_slice(&(u64::MAX / 2).to_be_bytes());
    let crc = rangecode::format::crc32(&blob[..24]);
    blob[24..28].copy_from_slice(&crc.to_be_bytes());
    let err = decode_container(&blob, &Budgets::default()).unwrap_err();
    assert!(matches!(
        err,
        rangecode::error::ContainerError::BudgetExceeded { .. }
    ));
}

#[test]
fn zero_symbol_chunk_writing_is_rejected() {
    // The writer refuses zero-symbol chunk specs through the public path:
    // an empty input yields zero chunks rather than a degenerate frame.
    let table = FreqTable::uniform(4, 16).unwrap();
    let blob = rangecode::container::encode_static(&[], &table, 4).unwrap();
    let p = decode_container(&blob, &Budgets::default()).unwrap();
    assert!(p.chunks.is_empty());
    assert_eq!(p.declared_symbols, 0);
}

#[test]
fn decoder_value_never_exceeds_alphabet_even_on_corruption() {
    // Whatever garbage arrives, every Ok symbol must be a valid table index.
    let mut rng = Lcg(0xABCDE);
    let table = FreqTable::new(&[3, 1, 4, 1, 5, 9, 2], 64).unwrap();
    let alpha = table.len() as u32;
    for _ in 0..20_000 {
        let n = 5 + rng.below(100);
        let mut payload = vec![0u8; n];
        for b in payload.iter_mut() {
            *b = (rng.next_u64() & 0xFF) as u8;
        }
        payload[0] = 0;
        if let Ok(mut dec) = RangeDecoder::new(&payload) {
            for _ in 0..10 {
                match dec.decode(&table) {
                    Ok(s) => assert!(s < alpha, "out-of-alphabet symbol {s}"),
                    Err(_) => break,
                }
            }
        }
    }
    // Also exercise decode_vec helper on short inputs.
    let _ = decode_vec(&table, &[0, 1, 2, 3], 4);
}
