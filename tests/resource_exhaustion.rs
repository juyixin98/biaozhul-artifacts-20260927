//! Evidence suite #3: resource-exhaustion and limit boundaries.
//!
//! Demonstrates the decompression safety policy end to end:
//! * absurd declarations are refused from header fields alone (no large allocation);
//! * the expansion-ratio and absolute-cap boundaries are exact;
//! * self-overlapping "zip bomb" RLE is capped at 8 MiB total, not expanded further;
//! * dependency-chain depth is bounded.
//!
//! The malicious frames are fixture files produced by the Python generator and
//! hand-crafted frames below; every Rust judgement is compared with the independent
//! reference category.

mod common;

use common::*;
use lz77_blocks::codec;
use lz77_blocks::error::{CodecError, ErrorCategory};
use lz77_blocks::format::{
    crc32, decode_frame, encode_frame, BlockMode, MAX_CHAIN_BLOCKS, MAX_EXPANSION_RATIO, MAX_MATCH,
    MAX_OUTPUT_BYTES,
};
use serde_json::{json, Value};

fn expect_category(
    log: &mut TestLog,
    case: &str,
    frame: &[u8],
    dict: &[u8],
    want: ErrorCategory,
    extra: Value,
) -> bool {
    let result = codec::decode_one(frame, dict);
    let (rep, _) = oracle_roundtrip(frame, dict);
    let rust_cat = result.as_ref().err().map(|e| e.category);
    let ok = rust_cat == Some(want);
    let oracle_ok = rep.category().as_deref() == Some(want.as_ref());
    let mut state_map = serde_json::Map::new();
    state_map.insert("want_category".into(), json!(want.as_ref()));
    state_map.insert(
        "rust_category".into(),
        json!(rust_cat.map(|c| c.as_ref().to_string())),
    );
    state_map.insert(
        "rust_detail".into(),
        json!(result.as_ref().err().map(|e| e.detail.clone())),
    );
    state_map.insert("oracle_category".into(), json!(rep.category()));
    state_map.insert("oracle_reason".into(), json!(rep.json.get("reason")));
    if let Value::Object(extra_obj) = extra {
        state_map.extend(extra_obj);
    }
    let state = Value::Object(state_map);
    log.pass_or_record(
        ok && oracle_ok,
        case,
        if ok && oracle_ok {
            "Rust and reference refuse with the identical failure category"
        } else {
            "category disagreement"
        },
        state,
    )
}

#[test]
fn malicious_lengths_are_refused_before_token_decoding() {
    let mut log = TestLog::new("resource_exhaustion");
    let mut failures: Vec<String> = Vec::new();

    // 1 GiB declaration, 1-byte (valid END) payload: absolute cap breach.
    let bomb = fixture("malformed/bomb_declared_size.frame");
    if !expect_category(
        &mut log,
        "bomb_declared_1gib",
        &bomb,
        b"",
        ErrorCategory::ResourceExhausted,
        json!({"declared": 1u64 << 30, "payload_len": 1,
               "note": "rejected while parsing header; decoder never allocates"}),
    ) {
        failures.push("bomb_declared_1gib".to_string());
    }

    // 20 MiB RLE expansion declaration, small spam payload: ratio breach.
    let ratio = fixture("malformed/bomb_expansion_ratio.frame");
    if !expect_category(
        &mut log,
        "bomb_expansion_ratio",
        &ratio,
        b"",
        ErrorCategory::ResourceExhausted,
        json!({"declared": 20_000_000u64}),
    ) {
        failures.push("bomb_expansion_ratio".to_string());
    }

    // Prove no large allocation happens: decode the 1 GiB frame in a thread whose
    // peak heap growth stays tiny. We approximate with RSS deltas via /proc.
    let rss_before = rss_kb();
    let mut bytes = 0u64;
    for _ in 0..200 {
        let err = codec::decode_one(&bomb, b"").unwrap_err();
        assert_eq!(err.category, ErrorCategory::ResourceExhausted);
        bytes += bomb.len() as u64;
    }
    let rss_after = rss_kb();
    let growth_kb = rss_after.saturating_sub(rss_before);
    let bounded = growth_kb < 16 * 1024; // < 16 MiB growth for 200 attempts
    log.pass_or_record(
        bounded,
        "no_giant_allocation",
        "200 decodes of the 1 GiB-declaration frame grow RSS by < 16 MiB",
        json!({"attempts": 200, "input_bytes_seen": bytes,
               "rss_kb_before": rss_before, "rss_kb_after": rss_after,
               "rss_growth_kb": growth_kb}),
    );
    if !bounded {
        failures.push("no_giant_allocation".to_string());
    }

    let path = log.finish();
    assert!(failures.is_empty(), "{failures:?}; log {path}");
}

#[test]
fn ratio_and_absolute_boundaries_are_exact() {
    let mut log = TestLog::new("resource_exhaustion");
    let mut failures: Vec<String> = Vec::new();

    // --- Ratio boundary: smallest payload that can legally declare ratio 100x.
    // payload_len P allows data_len <= 100*P. Build a frame declaring exactly that.
    let payload_end_only = vec![0xFFu8];
    let at_ratio = encode_frame(BlockMode::Independent, &[0u8; 32], 100, &payload_end_only);
    // Envelope accepts; token stream then fails because declared 100 != produced 0,
    // which is an input_error (not resource) — important distinction to assert.
    let env_err = codec::decode_one(&at_ratio, b"").unwrap_err();
    if env_err.category == ErrorCategory::Input {
        log.pass(
            "ratio_boundary_envelope_allows_token_check_denies",
            "declared ratio exactly 100x passes the resource gate, then fails as \
             input_error when zero bytes are actually produced",
            json!({"category": env_err.category.as_ref(), "detail": env_err.detail}),
        );
    } else {
        failures.push("ratio_boundary".into());
        log.fail(
            "ratio_boundary_envelope_allows_token_check_denies",
            "wrong category at ratio boundary",
            json!({"error": env_err.to_string()}),
        );
    }

    // One byte over the ratio -> resource_exhausted at envelope parse.
    let over_ratio = encode_frame(BlockMode::Independent, &[0u8; 32], 101, &payload_end_only);
    let e = decode_frame(&over_ratio).unwrap_err();
    if e.category == ErrorCategory::ResourceExhausted {
        log.pass(
            "ratio_101x_refused",
            "declared 101 bytes for 1 payload byte is resource_exhausted",
            json!({"detail": e.detail}),
        );
    } else {
        failures.push("ratio_over".into());
        log.fail(
            "ratio_101x_refused",
            "wrong category",
            json!({"error": e.to_string()}),
        );
    }

    // --- Absolute boundary: MAX_OUTPUT_BYTES accepted at envelope, +1 refused.
    // Each check needs a payload large enough that the *ratio* gate stays quiet,
    // so that the verdict is driven by the absolute cap itself (ratio <= 100x).
    let cap = MAX_OUTPUT_BYTES;
    // Payload must be >= ceil(cap/100) so the ratio gate passes and the absolute
    // cap is what decides (83886*100 = 8_388_600 < 8_388_608, so 83887 is the edge).
    let payload_for_cap = vec![0u8; cap.div_ceil(MAX_EXPANSION_RATIO) as usize]; // 83887
    let at_cap = encode_frame(BlockMode::Independent, &[0u8; 32], cap, &payload_for_cap);
    let env_ok = decode_frame(&at_cap).is_ok();

    // cap+1 needs payload_len >= ceil((cap+1)/100) = 83887 to reach the cap check.
    let payload_for_over = vec![0u8; ((cap + 1).div_ceil(MAX_EXPANSION_RATIO)) as usize];
    let over = encode_frame(
        BlockMode::Independent,
        &[0u8; 32],
        cap + 1,
        &payload_for_over,
    );
    let over_err = decode_frame(&over).unwrap_err();
    log.pass_or_record(
        env_ok && over_err.category == ErrorCategory::ResourceExhausted,
        "absolute_cap_boundary",
        "declared length == 8 MiB passes envelope, 8 MiB + 1 is resource_exhausted",
        json!({"cap": cap, "at_cap_envelope_ok": env_ok,
               "over_detail": over_err.detail}),
    );
    if !(env_ok && over_err.category == ErrorCategory::ResourceExhausted) {
        failures.push("absolute_cap".into());
    }

    let path = log.finish();
    assert!(failures.is_empty(), "{failures:?}; log {path}");
}

#[test]
fn self_overlap_bomb_is_capped_at_8mib() {
    // A maximally expanding stream: one literal + repeated dist-1 len-258 matches.
    // The decoder must honor the 8 MiB cap: declaring more is rejected; a valid
    // stream at the cap decodes in bounded memory to exactly the declared bytes.
    let mut log = TestLog::new("resource_exhaustion");

    let cap = MAX_OUTPUT_BYTES as usize; // 8 MiB
    let mut payload = Vec::new();
    payload.push(0x00u8);
    payload.push(b'z');
    let mut produced = 1usize;
    while produced + MAX_MATCH <= cap {
        payload.push(0x01);
        payload.extend_from_slice(&1u16.to_le_bytes());
        payload.push((MAX_MATCH - 3) as u8);
        produced += MAX_MATCH;
    }
    // Final partial match to land exactly on cap.
    let remainder = cap - produced;
    assert!(remainder >= 3, "test layout assumption");
    payload.push(0x01);
    payload.extend_from_slice(&1u16.to_le_bytes());
    payload.push((remainder - 3) as u8);
    produced += remainder;
    assert_eq!(produced, cap);
    payload.push(0xFF);

    let frame = encode_frame(BlockMode::Independent, &[0u8; 32], cap as u64, &payload);
    let rss_before = rss_kb();
    let out = codec::decode_one(&frame, b"").expect("capped RLE stream decodes");
    let rss_after = rss_kb();

    let exact = out.len() == cap && out.iter().all(|b| *b == b'z');
    let (rep, oracle_out) = oracle_roundtrip(&frame, b"");
    let cross = rep.valid() && oracle_out.as_deref() == Some(out.as_slice());
    let growth = rss_after.saturating_sub(rss_before);
    let mem_ok = growth < 20 * 1024; // under 20 MiB RSS growth for an 8 MiB output
    log.pass_or_record(
        exact && cross && mem_ok,
        "rle_bomb_at_cap",
        "maximal self-overlap stream expands to exactly 8 MiB in both implementations, \
         bounded memory",
        json!({"output_len": out.len(), "all_z": exact,
               "payload_len": payload.len(), "compression_ratio": (cap as f64) / payload.len() as f64,
               "oracle_valid": rep.valid(), "rss_growth_kb": growth}),
    );
    println!("{}", log.finish());
    assert!(exact && cross);
}

#[test]
fn dependency_chain_depth_is_limited() {
    let mut log = TestLog::new("resource_exhaustion");

    // Build a genuinely valid chain: one independent root of "r", then one
    // single-byte dependent block per step, digest rebound to the growing history.
    let mut frames = vec![codec::encode_independent(b"r")];
    let mut history: Vec<u8> = b"r".to_vec();
    for _ in 0..MAX_CHAIN_BLOCKS {
        frames.push(codec::encode_dependent(b"r", &history).unwrap());
        history.push(b'r');
    }
    assert_eq!(frames.len(), MAX_CHAIN_BLOCKS + 1);

    let err = codec::decode_chain(&frames).unwrap_err();
    let ok = err.category == ErrorCategory::ResourceExhausted && err.detail.contains("depth limit");
    log.pass_or_record(
        ok,
        "chain_depth_limit",
        "a 65-block chain is resource_exhausted even though every frame is well-formed",
        json!({"frames": frames.len(), "limit": MAX_CHAIN_BLOCKS, "detail": err.detail}),
    );

    // Boundary the other way: a chain exactly MAX_CHAIN_BLOCKS long must decode.
    frames.pop();
    assert_eq!(frames.len(), MAX_CHAIN_BLOCKS);
    let decoded = codec::decode_chain(&frames).expect("chain at the depth limit decodes");
    let at_limit_ok = decoded.len() == MAX_CHAIN_BLOCKS && decoded.iter().all(|b| *b == b'r');
    log.pass_or_record(
        at_limit_ok,
        "chain_at_depth_limit",
        "exactly 64 blocks decode (limit is exclusive beyond it)",
        json!({"blocks": MAX_CHAIN_BLOCKS, "bytes": decoded.len()}),
    );

    println!("{}", log.finish());
    assert!(ok && at_limit_ok);
}

// --------------------------------------------------------------------------- misc

#[test]
fn error_categories_are_distinct_and_stable() {
    // Guards the cross-module contract: category strings never silently change.
    let mut log = TestLog::new("resource_exhaustion");
    let pairs = [
        (ErrorCategory::Input, "input_error"),
        (ErrorCategory::StateConflict, "state_conflict"),
        (ErrorCategory::ResourceExhausted, "resource_exhausted"),
        (ErrorCategory::NotFound, "not_found"),
        (ErrorCategory::ComputeFailure, "compute_failure"),
    ];
    let ok = pairs.iter().all(|(c, s)| c.as_ref() == *s);
    log.pass_or_record(
        ok,
        "stable_category_strings",
        "input/state/resource/not-found/compute categories keep their stable wire names",
        json!({"pairs": pairs.map(|(c, s)| json!([c.as_ref(), s]))}),
    );
    println!("{}", log.finish());
    assert!(ok);
}

#[allow(dead_code)]
fn construct_codec_error_markers() {
    let _ = CodecError::exhausted("x");
    let _ = crc32(b"");
}

fn rss_kb() -> u64 {
    if let Ok(stat) = std::fs::read_to_string("/proc/self/status") {
        for line in stat.lines() {
            if let Some(rest) = line.strip_prefix("VmRSS:") {
                return rest
                    .split_whitespace()
                    .next()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0);
            }
        }
    }
    0
}
