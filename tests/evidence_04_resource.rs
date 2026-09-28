//! Evidence 04 — resource exhaustion. A decoder must distinguish:
//! * [`Code::OutputCapExceeded`]    — declared output over the per-block cap;
//! * [`Code::ExpansionCapExceeded`] — declared ratio over the expansion cap;
//! * [`Code::PayloadCapExceeded`]   — compressed payload too large;
//! * [`Code::TotalCapExceeded`]     — store aggregate cap.
//!
//! Critically, rejection happens from the header / before allocation, so the
//! process must not allocate gigabytes. We assert peak heap stays bounded by
//! measuring RSS around the rejected decode (best effort on Linux).

mod common;

use common::*;
use lz77b::core::constants::{HEADER_LEN, MAX_EXPANSION, MAX_OUTPUT, MAX_PAYLOAD};
use lz77b::core::decoder::ChainSession;
use lz77b::core::error::{Category, Code};
use lz77b::core::format::{BlockHeader, FrameType};
use lz77b::reference;
use lz77b::store::BlockStore;

fn forge(
    frame: FrameType,
    index: u32,
    prev: u64,
    payload: &[u8],
    declared: u64,
    crc: Option<u32>,
) -> Vec<u8> {
    let mut raw = Vec::new();
    BlockHeader {
        frame_type: frame,
        index,
        prev_digest: prev,
        payload_crc: crc.unwrap_or_else(|| lz77b::core::checksum::crc32(payload)),
        decompressed_len: declared,
    }
    .encode(&mut raw);
    raw.extend_from_slice(payload);
    raw
}

fn rss_kb() -> Option<u64> {
    let s = std::fs::read_to_string("/proc/self/status").ok()?;
    s.lines()
        .find(|l| l.starts_with("VmRSS:"))
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|x| x.parse().ok())
}

#[test]
fn declared_huge_output_is_rejected_at_header() {
    let mut log = RunRecorder::start("04-output-cap");
    let payload = [0x02u8]; // just END
    let raw = forge(
        FrameType::Independent,
        0,
        0,
        &payload,
        4u64 * 1024 * 1024 * 1024,
        None,
    );
    let before = rss_kb();
    let err = BlockHeader::decode(&raw).unwrap_err();
    let after = rss_kb();
    log.state("rss-before-kb", before.unwrap_or(0));
    log.state("rss-after-kb", after.unwrap_or(0));
    assert_eq!(err.code, Code::OutputCapExceeded);
    assert_eq!(err.category(), Category::Resource);
    if let (Some(b), Some(a)) = (before, after) {
        let delta = a.saturating_sub(b);
        log.state("rss-delta-kb", delta);
        // No multi-GB allocation: allow generous 64 MiB slack for test process noise.
        assert!(
            delta < 64 * 1024,
            "rejecting a 4 GiB claim grew RSS by {delta} KiB"
        );
    }
    // Independent reference must reject it the same way.
    let referr = reference::parse_header(&raw).unwrap_err();
    assert_eq!(referr.kind, reference::RefErrorKind::Resource);
    log.finish(true);
}

#[test]
fn expansion_bomb_is_distinct_from_output_cap() {
    let mut log = RunRecorder::start("04-expansion-cap");
    // A small valid payload claiming near-MAX_OUTPUT: total bytes under cap but
    // ratio far over MAX_EXPANSION.
    let payload = [0x02u8];
    let claim = (MAX_EXPANSION as u64 + 10) * (payload.len() as u64 + 1);
    assert!(
        claim <= MAX_OUTPUT as u64,
        "claim must stay under output cap to isolate the ratio rule"
    );
    let raw = forge(FrameType::Independent, 0, 0, &payload, claim, None);
    let err = BlockHeader::decode(&raw).unwrap_err();
    log.state("claimed bytes", claim);
    log.state("code", err.code_name());
    assert_eq!(err.code, Code::ExpansionCapExceeded);
    assert_eq!(err.category(), Category::Resource);

    let referr = reference::parse_header(&raw).unwrap_err();
    assert_eq!(referr.kind, reference::RefErrorKind::Resource);
    log.finish(true);
}

#[test]
fn oversized_payload_is_rejected() {
    let mut log = RunRecorder::start("04-payload-cap");
    // Header only, then claim > MAX_PAYLOAD bytes by appending zeroes.
    let mut raw = Vec::new();
    BlockHeader {
        frame_type: FrameType::Independent,
        index: 0,
        prev_digest: 0,
        payload_crc: 0, // irrelevant, rejected before CRC
        decompressed_len: 0,
    }
    .encode(&mut raw);
    raw.extend(std::iter::repeat(0u8).take(MAX_PAYLOAD + 1));
    let err = BlockHeader::decode(&raw).unwrap_err();
    log.state("payload bytes", raw.len() - HEADER_LEN);
    assert_eq!(err.code, Code::PayloadCapExceeded);
    assert_eq!(err.category(), Category::Resource);

    let referr = reference::parse_header(&raw).unwrap_err();
    assert_eq!(referr.kind, reference::RefErrorKind::Resource);
    log.finish(true);
}

#[test]
fn runtime_overrun_beyond_declared_length_is_caught_per_byte() {
    let mut log = RunRecorder::start("04-runtime-overrun");
    // Header claims 4 bytes; payload emits one literal then a distance-1 match
    // of length 200. The per-append reserve check must stop it once output
    // passes 4 bytes — before producing the full overlapping run.
    let mut leb = Vec::new();
    let write_leb = |buf: &mut Vec<u8>, mut v: u64| loop {
        let mut b = (v & 0x7f) as u8;
        v >>= 7;
        if v != 0 {
            b |= 0x80;
        }
        buf.push(b);
        if v == 0 {
            break;
        }
    };
    let mut payload = vec![0x00u8, 1u8, b'x', 0x01u8];
    write_leb(&mut leb, 1); // distance
    write_leb(&mut leb, 200 - 3); // length delta
    payload.extend_from_slice(&leb);
    payload.push(0x02);

    let raw = forge(FrameType::Independent, 0, 0, &payload, 4, None);
    let h = BlockHeader::decode(&raw).unwrap();
    let err = lz77b::core::decoder::decode_payload(
        FrameType::Independent,
        h.payload(&raw),
        &[],
        h.decompressed_len,
    )
    .unwrap_err();
    log.state("code", err.code_name());
    assert_eq!(err.code, Code::LengthMismatch);
    assert_eq!(err.category(), Category::Input);
    let referr = reference::decompress_raw(&raw, &[]).unwrap_err();
    assert_eq!(referr.kind, reference::RefErrorKind::Input);
    log.finish(true);
}

#[test]
fn store_total_cap_is_resource_and_leaves_no_partial_block() {
    let mut log = RunRecorder::start("04-store-total-cap");
    let dir = tempdir("total-cap");
    let store = BlockStore::open_with_cap(&dir, 32).unwrap();
    let s = ChainSession::new();
    let b0 = lz77b::core::decoder::encode_next(&s, b"cap cap cap cap cap").unwrap();
    let err = store.append_block("st", &b0.raw).unwrap_err();
    log.state("attempted bytes", b0.raw.len());
    assert_eq!(err.code, Code::TotalCapExceeded);
    assert_eq!(err.category(), Category::Resource);
    assert_eq!(store.block_count("st").unwrap_or(0), 0);
    // Only a possible leftover tmp file is allowed; no block file.
    let blocks: Vec<String> = std::fs::read_dir(dir.join("st"))
        .map(|rd| {
            rd.flatten()
                .map(|e| e.file_name().to_string_lossy().to_string())
                .collect()
        })
        .unwrap_or_default();
    log.assert_eq_display(
        "dir entries",
        blocks.join(","),
        String::new(),
        "no entries committed",
    );
    log.finish(true);
}

#[test]
fn chain_of_high_ratio_blocks_cannot_exceed_stream_output_cap() {
    // C1 regression: per-block caps (1 MiB output, on-disk cap) do NOT bound the
    // aggregate decompressed size of a whole stream. A chain of high-ratio
    // blocks must be stopped by the per-stream output cap before it materializes
    // unbounded memory.
    let mut log = RunRecorder::start("04-stream-output-cap");
    let dir = tempdir("stream-cap");
    // Generous disk cap (the bomb blocks are tiny), tiny decompressed cap.
    let store = BlockStore::open_with_caps(&dir, 1024 * 1024, 700 * 1024).unwrap();

    // One high-ratio block: a single repeated byte compresses to ~38 bytes but
    // expands to 512 KiB.
    let fill = vec![b'x'; 512 * 1024];
    let b0 = {
        let s = ChainSession::new();
        lz77b::core::decoder::encode_next(&s, &fill).unwrap().raw
    };
    log.state("block0 disk bytes", b0.len());
    assert!(b0.len() < 200, "bomb block must be tiny on disk");
    store.append_block("bomb", &b0).unwrap();

    // First block (512 KiB) fits the 700 KiB stream cap.
    assert_eq!(store.decode_stream("bomb").unwrap().len(), 512 * 1024);

    // A second such block would take the stream to 1 MiB > 700 KiB cap.
    let b1 = {
        let mut s = ChainSession::new();
        s.decode_raw(&b0).unwrap();
        lz77b::core::decoder::encode_next(&s, &fill).unwrap().raw
    };
    store.append_block("bomb", &b1).unwrap();
    log.state(
        "total disk bytes for 1 MiB decompressed",
        b0.len() + b1.len(),
    );
    let err = store.decode_stream("bomb").unwrap_err();
    log.state("code", err.code_name());
    assert_eq!(err.code, Code::StreamOutputCapExceeded);
    assert_eq!(err.category(), Category::Resource);
    log.note("two ~40-byte files expand to 1 MiB; decode is refused at the 700 KiB stream cap");
    log.finish(true);
}
