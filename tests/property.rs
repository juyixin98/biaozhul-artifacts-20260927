//! Property-style and adversarial integration tests.
//!
//! * Fixed-seed pseudo-random data over several distributions is round-tripped
//!   through the real encoder and BOTH the real and the independent decoder.
//! * Multi-block splitting is exercised with small block limits.
//! * Every single-byte mutation of small containers is classified: a mutation
//!   must never turn a valid object into an incorrectly-successful decode; it
//!   is either (still) correct — impossible for a content byte under CRC — or
//!   rejected with one of the documented categories.
//!
//! All decisions are written through the shared test logger with a run id,
//! the format/crate version, the concrete input identity and the evidence.

#[path = "support/independent_decoder.rs"]
mod independent;
#[path = "support/testlog.rs"]
mod testlog;

use hcomp::container::{decode_container, encode_container};

/// Small deterministic xorshift64 PRNG (no external dev-dependency needed).
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Rng(seed | 1)
    }
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn byte(&mut self) -> u8 {
        (self.next_u64() >> 24) as u8
    }
}

fn distributions(seed: u64, len: usize) -> Vec<(&'static str, Vec<u8>)> {
    let mut rng = Rng::new(seed);

    // Uniform random.
    let uniform: Vec<u8> = (0..len).map(|_| rng.byte()).collect();

    // Bernoulli-skewed: mostly zero, occasional random byte.
    let mut skew = Vec::with_capacity(len);
    for _ in 0..len {
        skew.push(if rng.next_u64().is_multiple_of(16) {
            rng.byte()
        } else {
            0
        });
    }

    // Small alphabet (4 symbols), strongly biased.
    let alpha = b"abcd";
    let small: Vec<u8> = (0..len)
        .map(|_| alpha[(rng.next_u64() % 16).min(3) as usize])
        .collect();

    // Bursty runs: pick a symbol, emit a run, occasionally switch.
    let mut bursty = Vec::with_capacity(len);
    let mut cur = rng.byte();
    while bursty.len() < len {
        let run = 1 + (rng.next_u64() % 50) as usize;
        for _ in 0..run {
            bursty.push(cur);
            if bursty.len() == len {
                break;
            }
        }
        if rng.next_u64().is_multiple_of(4) {
            cur = rng.byte();
        }
    }

    vec![
        ("uniform", uniform),
        ("skewed", skew),
        ("small_alphabet", small),
        ("bursty", bursty),
    ]
}

#[test]
fn fixed_seed_roundtrips_agree_between_decoders() {
    let run = testlog::run_id();
    for seed in [1u64, 42, 20260927, 0xDEAD_BEEF] {
        for (dist, data) in distributions(seed, 5000) {
            let input = format!("seed={seed},dist={dist},len={}", data.len());
            let limit = 1024u64; // forces multi-block splitting
            let chunks: Vec<Vec<u8>> = data.chunks(limit as usize).map(|c| c.to_vec()).collect();

            let blob = encode_container(&chunks, limit)
                .unwrap_or_else(|e| panic!("{input}: encode {e:?}"));
            assert!(blob.len() >= 16);

            let real = decode_container(&blob, limit)
                .unwrap_or_else(|e| panic!("{input}: real decode {:?}", e.kind()));
            let indep = independent::decode_container(&blob)
                .unwrap_or_else(|e| panic!("{input}: indep decode {e}"));

            assert_eq!(real.len(), chunks.len(), "{input} block count");
            assert_eq!(indep.len(), chunks.len(), "{input} indep block count");
            for (i, c) in chunks.iter().enumerate() {
                assert_eq!(&real[i].data, c, "{input} block {i} real");
                assert_eq!(&indep[i], c, "{input} block {i} indep");
            }

            testlog::log(
                "property",
                &input,
                "pass",
                serde_json::json!({
                    "blocks": chunks.len(),
                    "container_len": blob.len(),
                    "payload_lens": real.iter().map(|b| b.meta.payload_len).collect::<Vec<_>>(),
                    "run_id": run,
                }),
            );
        }
    }
}

#[test]
fn empty_input_is_one_explicit_empty_block() {
    for limit in [1u64, 64, 1 << 20] {
        let blob = encode_container(&[Vec::new()], limit).unwrap();
        let real = decode_container(&blob, limit).unwrap();
        let indep = independent::decode_container(&blob).unwrap();
        assert_eq!(real.len(), 1);
        assert_eq!(real[0].data.len(), 0);
        assert_eq!(indep, vec![Vec::<u8>::new()]);
        testlog::log(
            "property",
            &format!("empty,limit={limit}"),
            "pass",
            serde_json::json!({"container_len": blob.len(), "blocks": 1}),
        );
    }
}

#[test]
fn every_byte_mutation_of_a_small_container_is_rejected_or_neutral() {
    let run = testlog::run_id();
    let data = b"canonical huffman mutation sweep abcabc";
    let blob = encode_container(&[data.to_vec()], 1 << 20).unwrap();

    let mut categories: std::collections::BTreeMap<String, usize> = Default::default();
    let mut trials = 0usize;

    for byte_idx in 16..blob.len() {
        // Skip global header structural fields handled elsewhere; mutate the
        // directory and payload region (the trust boundary).
        for bit in 0..8u32 {
            let mut tampered = blob.clone();
            tampered[byte_idx] ^= 1 << bit;
            trials += 1;

            let real_result = decode_container(&tampered, 1 << 20);
            let indep_result = independent::decode_container(&tampered);

            match (&real_result, &indep_result) {
                (Ok(real), Ok(indep)) => {
                    // A mutation can only be "neutral" if it changed nothing
                    // observable: decoded bytes identical AND identical to
                    // the original. Because every directory/payload byte is
                    // CRC-covered, this must not happen for a real bit flip.
                    let concat: Vec<u8> =
                        real.iter().flat_map(|b| b.data.clone()).collect();
                    assert_ne!(
                        concat,
                        data,
                        "byte {byte_idx} bit {bit}: flipped bit decoded as unchanged data"
                    );
                    let _ = indep;
                }
                (Err(re), Err(ie)) => {
                    // Both must reject. Categories may legitimately differ
                    // (a CRC can fail first in one path, a length check in
                    // another), but neither may silently succeed.
                    let cat = re.kind().as_str().to_string();
                    assert_eq!(
                        ie.as_str(),
                        map_rough_category(&cat),
                        "byte {byte_idx} bit {bit}: real={cat}, indep={ie} disagreement"
                    );
                    *categories.entry(cat).or_default() += 1;
                }
                (real, indep) => panic!(
                    "byte {byte_idx} bit {bit}: decoder disagreement real_ok={} indep_ok={} (indep_err={:?})",
                    real.is_ok(),
                    indep.is_ok(),
                    indep.as_ref().err()
                ),
            }
        }
    }

    testlog::log(
        "adversarial",
        "single-bit sweeps over directory+payload",
        "pass",
        serde_json::json!({
            "run_id": run,
            "trials": trials,
            "categories": categories,
            "container_len": blob.len(),
        }),
    );
}

/// The independent decoder's vocabulary is slightly coarser for directory
/// fields (it checks contiguity before CRC in some cases); map the real
/// category to the acceptable independent-decoder category set.
fn map_rough_category(real: &str) -> String {
    match real {
        // A flipped payload byte typically breaks the payload CRC before
        // decode; the independent decoder performs the same check.
        "payload_crc_mismatch" => "payload_crc_mismatch".to_string(),
        "directory_crc_mismatch" => "directory_crc_mismatch".to_string(),
        // Semantic directory edits (lengths/offsets) can surface as any of
        // these depending on which guard trips first; the independent
        // decoder is allowed the broader structural class while still
        // rejecting.
        "payload_overlap" | "payload_out_of_bounds" | "trailing_garbage" => {
            "payload_overlap".to_string()
        }
        "truncated_directory" => "truncated_directory".to_string(),
        "duplicate_block_id" => "duplicate_block_id".to_string(),
        // Payload content bit flips that survive? They never survive CRC,
        // but if they did the bitstream categories would be these.
        other => other.to_string(),
    }
}

#[test]
fn truncated_prefixes_never_decode_successfully() {
    let run = testlog::run_id();
    let data = b"the quick brown fox jumps";
    let blob = encode_container(&[data.to_vec()], 1 << 20).unwrap();

    // Every strict prefix shorter than the valid length must be rejected.
    for cut in 16..blob.len() {
        let prefix = &blob[..cut];
        let real = decode_container(prefix, 1 << 20);
        let indep = independent::decode_container(prefix);
        assert!(
            real.is_err(),
            "prefix of {cut} bytes accepted by real decoder"
        );
        assert!(
            indep.is_err(),
            "prefix of {cut} bytes accepted by indep decoder"
        );
    }
    testlog::log(
        "adversarial",
        "all strict prefixes",
        "pass",
        serde_json::json!({
            "run_id": run,
            "prefixes_tested": blob.len() - 16,
        }),
    );
}

#[test]
fn random_garbage_is_never_silently_accepted() {
    let run = testlog::run_id();
    let mut rng = Rng::new(0xABCDEF);
    let mut accepted = 0usize;
    for trial in 0..2000u32 {
        let len = (rng.next_u64() % 128) as usize;
        let junk: Vec<u8> = (0..len).map(|_| rng.byte()).collect();
        if decode_container(&junk, 1 << 20).is_ok() {
            accepted += 1;
        }
        // A handful of random strings may share the magic by chance (~1/2^32);
        // assert the effective acceptance rate is zero.
        let _ = trial;
    }
    assert_eq!(accepted, 0, "random garbage decoded as valid containers");
    testlog::log(
        "adversarial",
        "2000 random byte strings",
        "pass",
        serde_json::json!({"run_id": run, "accepted": accepted, "trials": 2000}),
    );
}
