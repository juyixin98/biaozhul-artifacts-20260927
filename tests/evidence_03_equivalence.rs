//! Evidence 03 — chunking-mode equivalence and the hand-authored fixture
//! chain. The same input encoded at different block boundaries must decode to
//! identical bytes under both the core decoder and the independent reference.

mod common;

use common::*;
use lz77b::core::constants::WINDOW_SIZE;
use lz77b::core::decoder::ChainSession;
use lz77b::core::format::BlockHeader;
use lz77b::reference;

/// Decode a chain with a fresh core session.
fn core_decode_chain(raws: &[Vec<u8>]) -> Vec<u8> {
    let mut s = ChainSession::new();
    let mut all = Vec::new();
    for r in raws {
        all.extend_from_slice(&s.decode_raw(r).unwrap());
    }
    all
}

/// Decode a chain with the independent reference decompressor, threading the
/// rolling dictionary manually — the reference never sees the core decoder.
fn reference_decode_chain(raws: &[Vec<u8>]) -> Vec<u8> {
    let mut dict: Vec<u8> = Vec::new();
    let mut all = Vec::new();
    for (expected_index, raw) in raws.iter().enumerate() {
        let h = reference::parse_header(raw).unwrap();
        assert_eq!(
            h.index as usize, expected_index,
            "reference-side index continuity"
        );
        let part = reference::decompress_raw(raw, &dict).unwrap();
        // Mirror the dictionary rollover independently.
        let total = dict.len() + part.len();
        let take = total.min(WINDOW_SIZE);
        let mut combined = Vec::with_capacity(total);
        combined.extend_from_slice(&dict);
        combined.extend_from_slice(&part);
        dict = combined[total - take..].to_vec();
        all.extend_from_slice(&part);
    }
    all
}

#[test]
fn independent_single_block_matches_multi_block_chunks() {
    let mut log = RunRecorder::start("03-chunk-equivalence");
    let input = fixture("sample1.bin");
    log.state("input bytes", input.len());
    log.note("sample1 forces distance-1/2 overlaps and a cross-window phrase");

    // Mode A: single independent block (only if within payload cap; the sample
    // is 4.5 KiB and highly compressible).
    let single = encode_indep(&input);
    let h = BlockHeader::decode(&single).unwrap();
    log.state("single payload bytes", h.payload(&single).len());

    // Modes B..E: different fixed chunk boundaries.
    let modes = [16usize, 100, 1024, WINDOW_SIZE];
    let mut mode_outputs: Vec<(usize, Vec<Vec<u8>>)> = Vec::new();
    for &chunk in &modes {
        let (raws, _dict) = encode_at(&input, chunk);
        log.state(&format!("chunk={chunk} block count"), raws.len());
        for (i, r) in raws.iter().enumerate() {
            let hh = BlockHeader::decode(r).unwrap();
            log.state(
                &format!("chunk={chunk} block {i}"),
                format!(
                    "frame={} declared={} total={}",
                    hh.frame_type as u8,
                    hh.decompressed_len,
                    r.len()
                ),
            );
        }
        mode_outputs.push((chunk, raws));
    }

    // Reference decodes single independent block.
    let ref_single = reference::decompress_raw(&single, &[]).unwrap();
    log.assert_eq_display(
        "single-block reference length",
        ref_single.len(),
        input.len(),
        "single independent block fully decoded by independent implementation",
    );
    if ref_single != input {
        log.fail(
            "single-block reference bytes",
            "identical",
            "differ",
            "divergence",
        );
    }

    for (chunk, raws) in mode_outputs {
        let core = core_decode_chain(&raws);
        let refe = reference_decode_chain(&raws);
        log.assert_eq_display(
            &format!("chunk={chunk} core length"),
            core.len(),
            input.len(),
            "boundaries must not change recovered length",
        );
        if !log.check(
            &format!("chunk={chunk} core bytes identical"),
            core == input,
            "identical",
            if core == input { "identical" } else { "DIFFER" },
            "different segmentation, same bytes",
        ) {
            log.fail(
                &format!("chunk={chunk} core"),
                "identical",
                "differ",
                "chained decode differs",
            );
        }
        if !log.check(
            &format!("chunk={chunk} reference agrees with core"),
            refe == core,
            "identical",
            if refe == core { "identical" } else { "DIFFER" },
            "independent implementation on chained blocks",
        ) {
            log.fail(
                &format!("chunk={chunk} reference"),
                "identical",
                "differ",
                "reference divergence",
            );
        }
    }

    // Cross-mode sanity is fully covered by the identical-bytes checks above:
    // equality is transitive, so all four chunkings agree with the single block.
    log.finish(true);
}

#[test]
fn handmade_fixture_chain_decodes_with_expected_plaintexts() {
    let mut log = RunRecorder::start("03-handmade-fixtures");
    log.note("These blocks were assembled by examples/gen_fixtures, not the core encoder.");

    let expected: serde_json::Value = serde_json::from_slice(
        &std::fs::read(manifest_dir().join("fixtures/blocks/expected.json")).unwrap(),
    )
    .unwrap();
    let exp0 = expected["block0"]["ascii"].as_str().unwrap().as_bytes();
    let exp1 = expected["block1"]["ascii"].as_str().unwrap().as_bytes();
    let exp2 = expected["block2"]["ascii"].as_str().unwrap().as_bytes();

    let r0 = fixture_block(0);
    let r1 = fixture_block(1);
    let r2 = fixture_block(2);

    // Independent implementation first (it never trusts core).
    let p0 = reference::decompress_raw(&r0, &[]).unwrap();
    log.assert_eq_display(
        "fixture block0 (reference)",
        p0.len(),
        exp0.len(),
        "pinned plaintext length",
    );
    assert_eq!(p0, exp0);
    let h0 = reference::parse_header(&r0).unwrap();
    assert_eq!(h0.frame, reference::RefFrame::Independent);

    // Manually thread the dictionary using the fixture's recorded digest rule.
    let dict0 = {
        let take = p0.len().min(WINDOW_SIZE);
        p0[p0.len() - take..].to_vec()
    };
    let p1 = reference::decompress_raw(&r1, &dict0).unwrap();
    assert_eq!(p1, exp1);
    let combined1: Vec<u8> = dict0.iter().chain(p1.iter()).copied().collect();
    let dict1 = combined1[combined1.len() - combined1.len().min(WINDOW_SIZE)..].to_vec();
    let p2 = reference::decompress_raw(&r2, &dict1).unwrap();
    assert_eq!(p2, exp2);

    log.state("block0 plaintext", String::from_utf8_lossy(&p0));
    log.state("block1 plaintext", String::from_utf8_lossy(&p1));
    log.state("block2 plaintext", String::from_utf8_lossy(&p2));

    // Concrete overlap checks inside pinned outputs.
    assert_eq!(p0.iter().filter(|&&b| b == b'j').count(), 13);
    log.note("block0 contains exactly 13 'j' bytes (1 literal + 12 overlap copies)");
    assert!(p2.starts_with(b"ABABABABAB"));
    log.note("block2 begins with the distance-2 overlap result ABABABABAB");

    // Now the whole fixture chain through a single core session.
    let mut s = ChainSession::new();
    let c0 = s.decode_raw(&r0).unwrap();
    let c1 = s.decode_raw(&r1).unwrap();
    let c2 = s.decode_raw(&r2).unwrap();
    assert_eq!((c0, c1, c2), (p0.clone(), p1.clone(), p2.clone()));
    log.note("core session and independent reference agree on all three fixture blocks");
    log.finish(true);
}

#[test]
fn sample_fixture_digest_is_pinned() {
    let mut log = RunRecorder::start("03-fixture-digest");
    let sample = fixture("sample1.bin");
    let meta: serde_json::Value = serde_json::from_slice(
        &std::fs::read(manifest_dir().join("fixtures/sample1.meta.json")).unwrap(),
    )
    .unwrap();
    let pinned_len = meta["len"].as_u64().unwrap() as usize;
    let pinned_fnv = u64::from_str_radix(
        meta["fnv1a64"].as_str().unwrap().trim_start_matches("0x"),
        16,
    )
    .unwrap();

    log.assert_eq_display(
        "fixture length",
        sample.len(),
        pinned_len,
        "fixture file unchanged",
    );
    let actual = lz77b::core::checksum::fnv1a64(&[&sample]);
    log.assert_eq_display(
        "fixture FNV-1a64",
        actual,
        pinned_fnv,
        "a changed fixture invalidates every evidence claim downstream",
    );
    log.finish(true);
}
