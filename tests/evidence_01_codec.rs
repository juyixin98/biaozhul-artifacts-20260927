//! Evidence 01 — overlap semantics, cross-window matches, and agreement with
//! the independent reference decompressor.
//!
//! Every assertion names concrete expected values and the failure class; every
//! intermediate state that matters (distance, length, dict sizes, byte counts)
//! is written to the run log so a failure can be replayed.

mod common;

use common::*;
use lz77b::core::constants::{MAX_MATCH, WINDOW_SIZE};
use lz77b::core::error::Category;
use lz77b::core::format::{BlockHeader, FrameType, Token, TokenParser};
use lz77b::reference::{self, RefErrorKind};

/// Find the first match token in a block payload, asserting it exists.
fn first_match(raw: &[u8]) -> (usize, usize) {
    let h = BlockHeader::decode(raw).unwrap();
    let mut p = TokenParser::new(h.payload(raw));
    while let Some(t) = p.next_token().unwrap() {
        if let Token::Match { distance, length } = t {
            return (distance, length);
        }
    }
    panic!("expected a match token");
}

#[test]
fn self_overlapping_long_matches_are_byte_exact() {
    let mut log = RunRecorder::start("01-self-overlap");
    log.note("Distance-1 runs force the decoder to read bytes it is itself writing.");

    for (name, byte, len) in [
        ("zeros", b'0', 100),
        ("Z", b'Z', 5_000),
        ("Q", b'q', 70_000),
    ] {
        let input = vec![byte; len];
        let raw = encode_indep(&input);
        let (dist, mlen) = first_match(&raw);

        if !log.check(
            &format!("{name}: forced distance 1"),
            dist == 1,
            "1",
            &dist.to_string(),
            "a single-byte history can only be referenced at distance 1",
        ) {
            log.fail(
                &format!("{name} distance"),
                "1",
                &dist.to_string(),
                "distance not 1",
            );
        }
        log.state(&format!("{name}: longest match length"), mlen);
        if !log.check(
            &format!("{name}: long match emitted"),
            mlen >= 60,
            ">=60",
            &mlen.to_string(),
            "greedy parser must extend a distance-1 match far past the source",
        ) {
            log.fail(
                &format!("{name} length"),
                ">=60",
                &mlen.to_string(),
                "match too short",
            );
        }

        // Core decoder.
        let h = BlockHeader::decode(&raw).unwrap();
        let core = lz77b::core::decoder::decode_payload(
            FrameType::Independent,
            h.payload(&raw),
            &[],
            h.decompressed_len,
        )
        .unwrap();
        log.assert_eq_display(
            &format!("{name}: core byte length"),
            core.len(),
            input.len(),
            "overlap expansion must produce exactly the declared length",
        );
        if !log.check(
            &format!("{name}: core bytes exact"),
            core == input,
            "identical",
            "identical",
            "per-byte periodic reproduction",
        ) {
            log.fail(
                &format!("{name} core bytes"),
                "identical",
                "differ",
                "overlap semantics wrong",
            );
        }

        // Independent reference.
        let refe =
            reference::decompress_raw(&raw, &[]).unwrap_or_else(|e| panic!("reference: {e}"));
        if !log.check(
            &format!("{name}: reference agrees"),
            refe == core,
            "identical",
            if refe == core { "identical" } else { "DIFFER" },
            "two independent implementations must agree on overlap bytes",
        ) {
            log.fail(
                &format!("{name} reference"),
                "identical",
                "differ",
                "core/reference divergence",
            );
        }
    }

    // A distance-2 ABAB-style overlap, encoded core, decoded by both.
    let mut ab = Vec::new();
    for i in 0..1000 {
        ab.push(if i % 2 == 0 { b'A' } else { b'B' });
    }
    let raw = encode_indep(&ab);
    let (dist, mlen) = first_match(&raw);
    log.assert_eq_display("AB period distance", dist, 2, "two-byte period");
    log.state("AB period match length", mlen);
    let h = BlockHeader::decode(&raw).unwrap();
    let core = lz77b::core::decoder::decode_payload(
        FrameType::Independent,
        h.payload(&raw),
        &[],
        h.decompressed_len,
    )
    .unwrap();
    let refe = reference::decompress_raw(&raw, &[]).unwrap();
    assert_eq!(core, ab);
    assert_eq!(refe, ab);

    log.finish(true);
}

#[test]
fn matches_cross_the_window_boundary() {
    let mut log = RunRecorder::start("01-cross-window");
    log.note("A phrase appears before and after the 4 KiB sliding window edge.");

    let mut input = Vec::new();
    input.extend_from_slice(b"BOUNDARY-MARKER"); // 15 bytes, starts at 0
                                                 // deterministic low-entropy filler that still avoids accidental triples
    let mut x: u32 = 7;
    while input.len() < WINDOW_SIZE + 100 {
        x = x.wrapping_mul(1103515245).wrapping_add(12345);
        // bias toward a small alphabet so payload stays tiny but phrases differ
        input.push(b"abcd"[(x >> 16) as usize % 4]);
    }
    // Re-emit marker well after WINDOW_SIZE bytes: must NOT match (expired).
    input.extend_from_slice(b"BOUNDARY-MARKER");
    // Then an immediately-repeated phrase: must match inside the window.
    input.extend_from_slice(b"fresh-repeatable-phrase");
    input.extend_from_slice(b"fresh-repeatable-phrase");

    let raw = encode_indep(&input);
    let h = BlockHeader::decode(&raw).unwrap();
    let stats_marker = {
        // collect distances/lengths
        let payload = h.payload(&raw);
        let mut p = TokenParser::new(payload);
        let mut matches = Vec::new();
        while let Some(t) = p.next_token().unwrap() {
            if let Token::Match { distance, length } = t {
                matches.push((distance, length));
            }
        }
        matches
    };
    log.state("match tokens total", stats_marker.len());
    for (i, (d, l)) in stats_marker.iter().enumerate().take(12) {
        log.state(&format!("match[{i}] (distance,length)"), format!("{d},{l}"));
    }
    let max_distance = stats_marker.iter().map(|(d, _)| *d).max().unwrap_or(0);
    if !log.check(
        "no match distance exceeds WINDOW_SIZE",
        stats_marker.iter().all(|&(d, _)| d <= WINDOW_SIZE),
        &format!("<= {WINDOW_SIZE}"),
        &format!("max {}", max_distance),
        "expired history must never be referenced",
    ) {
        log.fail(
            "window bound",
            "all <= WINDOW",
            "one exceeded",
            "encoder window bug",
        );
    }
    let long_count = stats_marker.iter().filter(|&&(_, l)| l >= 20).count();
    if !log.check(
        "the in-window repeat produced a long match",
        long_count > 0,
        "a match >= 20",
        &format!("{long_count} found"),
        "the adjacent repeated phrase is within the window and must compress",
    ) {
        log.fail(
            "in-window repeat",
            ">=1 long match",
            "none",
            "match finder missed window data",
        );
    }

    let core = lz77b::core::decoder::decode_payload(
        FrameType::Independent,
        h.payload(&raw),
        &[],
        h.decompressed_len,
    )
    .unwrap();
    let refe = reference::decompress_raw(&raw, &[]).unwrap();
    assert_eq!(core, input);
    assert_eq!(refe, input);
    log.assert_eq_display(
        "decoded length",
        core.len(),
        input.len(),
        "boundary input fully recovered",
    );
    log.finish(true);
}

#[test]
fn dependent_block_pulls_across_block_boundary() {
    let mut log = RunRecorder::start("01-cross-block");

    // Block 0 ends with a distinctive phrase; block 1 repeats it immediately,
    // so the match source lives entirely in the predecessor dictionary.
    let b0_input = {
        let mut v = b"prefix ".repeat(10);
        v.extend_from_slice(b"PHRASE-FROM-BLOCK-ZERO");
        v
    };
    let raw0 = encode_indep(&b0_input);
    let h0 = BlockHeader::decode(&raw0).unwrap();
    let plain0 = lz77b::core::decoder::decode_payload(
        FrameType::Independent,
        h0.payload(&raw0),
        &[],
        h0.decompressed_len,
    )
    .unwrap();
    let dict = &plain0[plain0.len() - WINDOW_SIZE.min(plain0.len())..];

    let b1 = b"PHRASE-FROM-BLOCK-ZERO again";
    let raw1 = lz77b::core::encoder::encode_block(FrameType::Dependent, 1, dict, b1).unwrap();
    let (dist, mlen) = first_match(&raw1.raw);
    log.state("cross-block match distance", dist);
    log.state("cross-block match length", mlen);
    // The 24-byte phrase exists only in the predecessor dictionary; the match
    // source must reach back at least as far as the first produced literal.
    if !log.check(
        "first match reaches predecessor block",
        dist >= 20 && mlen >= 20,
        "distance>=20 and length>=20",
        &format!("distance={dist}, length={mlen}"),
        "the long phrase only exists in the predecessor dictionary",
    ) {
        log.fail(
            "cross-block",
            "into dict",
            "stayed in block 1",
            "match missed dictionary",
        );
    }

    let h1 = BlockHeader::decode(&raw1.raw).unwrap();
    let core = lz77b::core::decoder::decode_payload(
        FrameType::Dependent,
        h1.payload(&raw1.raw),
        dict,
        h1.decompressed_len,
    )
    .unwrap();
    let refe = reference::decompress_raw(&raw1.raw, dict).unwrap();
    assert_eq!(core, b1);
    assert_eq!(refe, b1);
    log.assert_eq_display(
        "dependent decoded length",
        core.len(),
        b1.len(),
        "block 1 recovered",
    );
    log.finish(true);
}

#[test]
fn max_match_boundary_is_respected() {
    let mut log = RunRecorder::start("01-max-match");
    // Input long enough that greedy could exceed MAX_MATCH: token deltas cap at
    // u16, so either it emits multiple matches, or one of exactly MAX_MATCH.
    let input = vec![b'x'; MAX_MATCH + 5_000];
    let raw = encode_indep(&input);
    let h = BlockHeader::decode(&raw).unwrap();
    let mut p = TokenParser::new(h.payload(&raw));
    let mut longest = 0;
    while let Some(t) = p.next_token().unwrap() {
        if let Token::Match { length, .. } = t {
            longest = longest.max(length);
        }
    }
    log.state("longest single match", longest);
    log.assert_eq_display(
        "no match above fixed MAX_MATCH",
        longest <= MAX_MATCH,
        true,
        "length field is 16-bit over MIN_MATCH",
    );
    let core = lz77b::core::decoder::decode_payload(
        FrameType::Independent,
        h.payload(&raw),
        &[],
        h.decompressed_len,
    )
    .unwrap();
    let refe = reference::decompress_raw(&raw, &[]).unwrap();
    assert_eq!(core, input);
    assert_eq!(refe, input);
    log.finish(true);
}

#[test]
fn invalid_distance_is_input_error_in_both_decoders() {
    let mut log = RunRecorder::start("01-bad-distance");
    // Hand-built token stream: literal 'a', then distance 100 with only 1 byte
    // of history, then END.
    let mut p = vec![0x00u8, 1, b'a', 0x01];
    leb(&mut p, 100);
    leb(&mut p, 0); // length MIN_MATCH
    p.push(0x02);

    let raw = wrap_block(FrameType::Independent, 0, 0, &p, 4);
    let core_err =
        lz77b::core::decoder::decode_payload(FrameType::Independent, &p, &[], 4).unwrap_err();
    log.state("core error code", core_err.code_name());
    log.state("core category", core_err.category().to_string());
    assert_eq!(core_err.category(), Category::Input);
    assert_eq!(core_err.code, lz77b::core::error::Code::BadDistance);

    let ref_err = reference::decompress_raw(&raw, &[]).unwrap_err();
    log.state("reference kind", format!("{:?}", ref_err.kind));
    assert_eq!(ref_err.kind, RefErrorKind::Input);
    log.note("both implementations classify an out-of-history distance as malformed input");
    log.finish(true);
}

fn leb(buf: &mut Vec<u8>, mut v: u64) {
    loop {
        let mut b = (v & 0x7f) as u8;
        v >>= 7;
        if v != 0 {
            b |= 0x80;
        }
        buf.push(b);
        if v == 0 {
            break;
        }
    }
}

/// Assemble a valid-CRC block around an arbitrary payload for tests.
fn wrap_block(frame: FrameType, index: u32, prev: u64, payload: &[u8], declared: u64) -> Vec<u8> {
    let mut raw = Vec::new();
    BlockHeader {
        frame_type: frame,
        index,
        prev_digest: prev,
        payload_crc: lz77b::core::checksum::crc32(payload),
        decompressed_len: declared,
    }
    .encode(&mut raw);
    raw.extend_from_slice(payload);
    raw
}

#[test]
fn giant_literal_length_is_an_error_not_a_panic_in_either_decoder() {
    // Regression for a review-found defect: the independent reference's
    // literal-length arithmetic (`pos + len`) wrapped for a u64::MAX length and
    // panicked instead of returning an error, while the core returned one.
    // Both must now reject, and they must agree.
    let mut log = RunRecorder::start("01-giant-literal");
    let mut payload = vec![0x00u8];
    // canonical LEB128 of u64::MAX
    payload.extend_from_slice(&[0xffu8; 9]);
    payload.push(0x01);

    // declared_len=0 so the block passes every *header* cap.
    let raw = wrap_block(FrameType::Independent, 0, 0, &payload, 0);
    let core_err =
        lz77b::core::decoder::decode_payload(FrameType::Independent, &payload, &[], 0).unwrap_err();
    log.state("core code", core_err.code_name());
    assert_eq!(core_err.category(), Category::Input);

    let ref_err =
        reference::decompress_raw(&raw, &[]).expect_err("reference must reject, not panic");
    log.state("reference kind", format!("{:?}", ref_err.kind));
    log.state("reference reason", &ref_err.reason);
    assert_eq!(ref_err.kind, RefErrorKind::Input);
    log.note(
        "a CRC-valid block claiming a u64::MAX literal run is rejected by both implementations",
    );
    log.finish(true);
}

#[test]
fn distance_beyond_window_is_rejected_even_with_history() {
    // A block that produced > WINDOW_SIZE bytes must not then be able to
    // reference beyond the fixed window, even though the bytes exist.
    let mut log = RunRecorder::start("01-distance-window-spec");
    let base: Vec<u8> = (0..(WINDOW_SIZE + 100)).map(|i| (i % 251) as u8).collect();
    let raw = encode_indep(&base);
    let h = BlockHeader::decode(&raw).unwrap();
    let dec_ok = lz77b::core::decoder::decode_payload(
        FrameType::Independent,
        h.payload(&raw),
        &[],
        h.decompressed_len,
    )
    .unwrap();
    assert_eq!(dec_ok, base);

    // Hand-forged match: after 8192 literals, reference distance 8000 (> window).
    let mut payload = vec![0x00u8];
    leb(&mut payload, (WINDOW_SIZE + 100) as u64);
    payload.extend_from_slice(&base);
    payload.push(0x01);
    let beyond = (WINDOW_SIZE as u64) + 3904;
    leb(&mut payload, beyond);
    leb(&mut payload, 0); // length MIN_MATCH
    payload.push(0x02);
    let declared = (WINDOW_SIZE + 100 + 3) as u64;
    let raw = wrap_block(FrameType::Independent, 0, 0, &payload, declared);
    let core_err =
        lz77b::core::decoder::decode_payload(FrameType::Independent, &payload, &[], declared)
            .unwrap_err();
    log.state("core code", core_err.code_name());
    assert_eq!(core_err.code, lz77b::core::error::Code::BadDistance);
    let ref_err = reference::decompress_raw(&raw, &[]).unwrap_err();
    assert_eq!(ref_err.kind, RefErrorKind::Input);
    log.note("distance above the fixed 4096-byte window is malformed even when the referenced byte exists in this block");
    log.finish(true);
}
