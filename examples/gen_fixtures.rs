//! Deterministic fixture generator.
//!
//! Run with `cargo run --example gen_fixtures`. Produces under `fixtures/`:
//!
//! - `sample1.bin`: crafted to exercise self-overlapping long matches
//!   (distance-1 runs, distance-2 patterns, phrases recurring across the
//!   4 KiB window boundary);
//! - `sample1.meta.json`: length and FNV digest, hard-pinned by the tests;
//! - `blocks/*.lzb`: a hand-assembled 3-block chain (independent + 2
//!   dependent) for cross-checking without any core encoder.
//!
//! The hand-assembled blocks are encoded here with *local* one-off LEB128 and
//! header code copied from neither core nor reference, so the fixture bytes
//! exist independently of the implementation under test.

use std::fs;
use std::path::PathBuf;

fn main() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures");
    fs::create_dir_all(root.join("blocks")).unwrap();

    let sample = build_sample();
    let digest = fnv1a64(&sample);
    fs::write(root.join("sample1.bin"), &sample).unwrap();
    fs::write(
        root.join("sample1.meta.json"),
        format!(
            "{{\"file\":\"sample1.bin\",\"len\":{},\"fnv1a64\":\"{:#018x}\"}}\n",
            sample.len(),
            digest
        ),
    )
    .unwrap();
    println!(
        "wrote fixtures/sample1.bin ({} bytes, fnv={:#018x})",
        sample.len(),
        digest
    );

    build_handmade_chain(&root);
    println!("wrote fixtures/blocks/block-0000000[0,1,2].lzb (hand-assembled chain)");
}

/// Construct input where the interesting decoder behaviours are *forced*:
/// * runs of one byte (distance 1, long overlapping copy);
/// * two-byte period "AB" (distance 2 overlap);
/// * a phrase first emitted, then > WINDOW_SIZE later, plus near the boundary.
fn build_sample() -> Vec<u8> {
    let mut v = Vec::new();

    // 1. self-overlap, distance 1: 60 'Z' (decoder must emit 59 by overlap)
    v.extend(std::iter::repeat(b'Z').take(60));

    // 2. self-overlap, distance 2: period "AB", 70 bytes
    for i in 0..70 {
        v.push(if i % 2 == 0 { b'A' } else { b'B' });
    }

    // 3. a marker phrase
    v.extend_from_slice(b"MARKER-PHRASE//");

    // 4. filler to push the marker out to (and across) the 4 KiB window edge
    while v.len() < 4090 {
        v.push(b'.');
    }

    // 5. repeating filler structure right across the window boundary
    for i in 0..40 {
        v.extend_from_slice(b"filler");
        v.push(b'0' + (i % 10) as u8);
    }

    // 6. the marker phrase again — > WINDOW_SIZE bytes after its first
    //    occurrence only partially; "filler" repeats are inside the window.
    v.extend_from_slice(b"MARKER-PHRASE//");

    // 7. final long distance-1 run to end on an overlap edge
    v.extend(std::iter::repeat(b'Q').take(200));

    v
}

// ---- hand-assembled block writer (local, deliberately independent) -------

fn leb(buf: &mut Vec<u8>, mut x: u64) {
    loop {
        let mut b = (x & 0x7f) as u8;
        x >>= 7;
        if x != 0 {
            b |= 0x80;
        }
        buf.push(b);
        if x == 0 {
            break;
        }
    }
}

fn crc32(data: &[u8]) -> u32 {
    let mut c = 0xFFFF_FFFFu32;
    for &x in data {
        c ^= u32::from(x);
        for _ in 0..8 {
            c = if c & 1 != 0 {
                (c >> 1) ^ 0xEDB8_8320
            } else {
                c >> 1
            };
        }
    }
    c ^ 0xFFFF_FFFF
}

fn fnv1a64(data: &[u8]) -> u64 {
    let mut h = 0xcbf2_9ce4_8422_2325u64;
    for &b in data {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

fn literal(buf: &mut Vec<u8>, bytes: &[u8]) {
    buf.push(0x00);
    leb(buf, bytes.len() as u64);
    buf.extend_from_slice(bytes);
}
fn match_(buf: &mut Vec<u8>, dist: u64, len: u64) {
    assert!(len >= 3);
    buf.push(0x01);
    leb(buf, dist);
    leb(buf, len - 3);
}
fn end(buf: &mut Vec<u8>) {
    buf.push(0x02);
}

fn dict_digest(index: u32, dict: &[u8]) -> u64 {
    // Mirror the format's domain-separated digest using local code.
    let mut h = 0xcbf2_9ce4_8422_2325u64;
    let mut eat = |bytes: &[u8]| {
        for &b in bytes {
            h ^= u64::from(b);
            h = h.wrapping_mul(0x0000_0100_0000_01b3);
        }
    };
    eat(b"LZDICT:1\n");
    eat(&index.to_le_bytes());
    eat(&(dict.len() as u64).to_le_bytes());
    eat(dict);
    h
}

fn write_block(dir: &std::path::Path, n: u32, frame: u8, prev: u64, payload: &[u8], declared: u64) {
    let mut raw = Vec::new();
    raw.extend_from_slice(b"LZ7B");
    raw.push(1);
    raw.push(frame);
    raw.extend_from_slice(&n.to_be_bytes());
    raw.extend_from_slice(&prev.to_be_bytes());
    raw.extend_from_slice(&crc32(payload).to_be_bytes());
    raw.extend_from_slice(&declared.to_be_bytes());
    raw.extend_from_slice(payload);
    fs::write(dir.join(format!("block-{n:08}.lzb")), &raw).unwrap();
}

fn build_handmade_chain(root: &std::path::Path) {
    let dir = root.join("blocks");

    // Block 0 (independent): literals then a self-overlap distance-1 match.
    let mut p0 = Vec::new();
    literal(&mut p0, b"abcdefghij"); // 10 literals
                                     // distance 1: repeat 'j' 12 more times -> total 22 'j' suffix? We emit a
                                     // match of length 12 referencing the single 'j'.
    match_(&mut p0, 1, 12);
    literal(&mut p0, b"tail-0");
    end(&mut p0);
    let plain0: Vec<u8> = b"abcdefghi"
        .iter()
        .chain(std::iter::repeat(&b'j').take(13))
        .chain(b"tail-0".iter())
        .copied()
        .collect();
    write_block(&dir, 0, 0, 0, &p0, plain0.len() as u64);

    let dict0: Vec<u8> = plain0[plain0.len().saturating_sub(4096)..].to_vec();

    // Block 1 (dependent): literals, then a match into block-0's dictionary,
    // then a distance-1 overlap extending its final byte.
    // Block0 tail is "...jjjjjjjjjjjjjtail-0"; after producing "xyz" (3 bytes)
    // the 't' of "tail-0" is 9 bytes back (6 dict bytes + 3 new bytes).
    let mut p1 = Vec::new();
    literal(&mut p1, b"xyz");
    match_(&mut p1, 9, 6); // copies "tail-0" entirely from the dictionary
    match_(&mut p1, 1, 4); // overlap: four copies of the new last byte '0'
    literal(&mut p1, b"one");
    end(&mut p1);
    let plain1: Vec<u8> = b"xyz"
        .iter()
        .chain(b"tail-0".iter())
        .chain(b"0000".iter())
        .chain(b"one".iter())
        .copied()
        .collect();
    let digest1 = dict_digest(1, &dict0);
    write_block(&dir, 1, 1, digest1, &p1, plain1.len() as u64);

    let combined1: Vec<u8> = dict0.iter().chain(plain1.iter()).copied().collect();
    let dict1: Vec<u8> = combined1[combined1.len().saturating_sub(4096)..].to_vec();

    // Block 2 (dependent): a distance-2 ABAB-style overlap built from its own
    // bytes, followed by "tail-0" pulled from the predecessor dictionary.
    // At the second match, 10 AB bytes have already been produced in this
    // block, so the distance to dict1's "tail-0" includes that offset.
    let mut p2 = Vec::new();
    literal(&mut p2, b"AB");
    match_(&mut p2, 2, 8); // ten-byte AB period via overlap at distance 2
    let hist: &[u8] = &dict1;
    let produced_so_far = 10u64;
    let tailpos = hist.windows(6).rposition(|w| w == b"tail-0").unwrap();
    let dist_tail = produced_so_far + (hist.len() - tailpos) as u64;
    match_(&mut p2, dist_tail, 6);
    end(&mut p2);
    // Expected plaintext: the 10-byte AB-period produced by an overlap match
    // at distance 2, followed by the phrase pulled across the window.
    let plain2: Vec<u8> = (0..10)
        .map(|i| if i % 2 == 0 { b'A' } else { b'B' })
        .chain(b"tail-0".iter().copied())
        .collect();
    let digest2 = dict_digest(2, &dict1);
    write_block(&dir, 2, 1, digest2, &p2, plain2.len() as u64);

    // Pin expected plaintexts so reviewers can decode by eye.
    fs::write(
        root.join("blocks").join("expected.json"),
        serde_json_unavailable(&[
            ("block0", plain0.as_slice()),
            ("block1", plain1.as_slice()),
            ("block2", plain2.as_slice()),
        ]),
    )
    .unwrap();
}

/// Tiny JSON emitter for pinned expected bytes (avoids a dev-dependency).
fn serde_json_unavailable(blocks: &[(&str, &[u8])]) -> String {
    let mut s = String::from("{\n");
    for (i, (name, bytes)) in blocks.iter().enumerate() {
        s.push_str(&format!(
            "  \"{name}\": {{\"len\": {}, \"ascii\": \"{}\"}}{}",
            bytes.len(),
            String::from_utf8_lossy(bytes)
                .replace('\\', "\\\\")
                .replace('"', "\\\""),
            if i + 1 < blocks.len() { "," } else { "" }
        ));
        s.push('\n');
    }
    s.push_str("}\n");
    s
}
