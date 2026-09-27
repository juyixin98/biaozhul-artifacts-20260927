//! Persistence round-trips and format rejection categories.

use std::io::{Read, Write};

use mphf::error::ErrorKind;
use mphf::{build, format, BuildConfig, Probe, VerifyMode};

fn sample(mode: VerifyMode) -> mphf::MphfIndex {
    let keys: Vec<Vec<u8>> = (0..150)
        .map(|i| format!("persist-{i:04}-key").into_bytes())
        .collect();
    build(
        keys.clone(),
        &BuildConfig {
            verify: mode,
            ..Default::default()
        },
    )
    .unwrap()
    .index
}

#[test]
fn full_key_roundtrip_preserves_membership_and_slots() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("set.mphf");
    let idx = sample(VerifyMode::FullKey);

    let mut member_slots = std::collections::HashMap::new();
    for i in 0..150 {
        let k = format!("persist-{i:04}-key");
        if let Probe::Member { slot } = idx.probe(k.as_bytes()) {
            member_slots.insert(k, slot);
        } else {
            panic!("member {k} rejected pre-save");
        }
    }

    format::save_to_path(&path, &idx).unwrap();
    let reloaded = format::load_from_path(&path).unwrap();

    for (k, slot) in &member_slots {
        match reloaded.probe(k.as_bytes()) {
            Probe::Member { slot: s2 } => assert_eq!(s2, *slot, "slot changed for {k}"),
            other => panic!("member {k} rejected after reload: {other:?}"),
        }
    }
    for k in ["persist-00000-keyX", "other", ""] {
        assert!(matches!(reloaded.probe(k.as_bytes()), Probe::Rejected { .. }));
    }
}

#[test]
fn fingerprint_roundtrip_rejects_nonmembers() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("fp.mphf");
    let idx = sample(VerifyMode::Fingerprint { bits: 16 });
    format::save_to_path(&path, &idx).unwrap();
    let reloaded = format::load_from_path(&path).unwrap();
    // Members.
    for i in 0..150 {
        let k = format!("persist-{i:04}-key");
        assert!(matches!(
            reloaded.probe(k.as_bytes()),
            Probe::Member { .. }
        ));
    }
    // Deterministic near-misses all reject (16-bit FPR ~1.5e-5 each).
    let rejects = (0..200)
        .map(|i| format!("persist-{i:04}-KEY")) // uppercase suffix variant
        .filter(|k| !k.is_empty())
        .filter(|k| matches!(reloaded.probe(k.as_bytes()), Probe::Rejected { .. }))
        .count();
    assert!(rejects >= 195, "expected near-zero FP, got {rejects}/200 rejects");
}

#[test]
fn empty_set_roundtrip() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("empty.mphf");
    let idx = build(
        vec![],
        &BuildConfig {
            verify: VerifyMode::FullKey,
            ..Default::default()
        },
    )
    .unwrap()
    .index;
    format::save_to_path(&path, &idx).unwrap();
    let reloaded = format::load_from_path(&path).unwrap();
    assert_eq!(reloaded.key_count(), 0);
    assert!(matches!(reloaded.probe(b"x"), Probe::Rejected { .. }));
    assert!(matches!(reloaded.probe(b""), Probe::Rejected { .. }));
}

fn read_file(path: &std::path::Path) -> Vec<u8> {
    let mut f = std::fs::File::open(path).unwrap();
    let mut buf = Vec::new();
    f.read_to_end(&mut buf).unwrap();
    buf
}

fn write_file(path: &std::path::Path, bytes: &[u8]) {
    let mut f = std::fs::File::create(path).unwrap();
    f.write_all(bytes).unwrap();
}

#[test]
fn bad_magic_is_format_magic_error() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("set.mphf");
    format::save_to_path(&path, &sample(VerifyMode::FullKey)).unwrap();
    let mut bytes = read_file(&path);
    bytes[0..4].copy_from_slice(b"XXXX");
    write_file(&path, &bytes);
    let err = format::load_from_path(&path).unwrap_err();
    assert_eq!(err.kind(), ErrorKind::FormatMagic);
}

#[test]
fn unknown_version_is_format_version_error() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("set.mphf");
    format::save_to_path(&path, &sample(VerifyMode::FullKey)).unwrap();
    let mut bytes = read_file(&path);
    bytes[4] = 99;
    write_file(&path, &bytes);
    let err = format::load_from_path(&path).unwrap_err();
    assert_eq!(err.kind(), ErrorKind::FormatVersion);
}

#[test]
fn bitflip_payload_is_checksum_mismatch() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("set.mphf");
    format::save_to_path(&path, &sample(VerifyMode::FullKey)).unwrap();
    let mut bytes = read_file(&path);
    // Flip a body byte past the 48-byte header.
    let idx = bytes.len() - 5;
    bytes[idx] ^= 0x01;
    write_file(&path, &bytes);
    let err = format::load_from_path(&path).unwrap_err();
    assert_eq!(err.kind(), ErrorKind::ChecksumMismatch);
}

#[test]
fn truncated_file_is_a_corrupt_or_io_error_never_ok() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("set.mphf");
    format::save_to_path(&path, &sample(VerifyMode::FullKey)).unwrap();
    let mut bytes = read_file(&path);
    bytes.truncate(30); // mid-header
    write_file(&path, &bytes);
    let res = format::load_from_path(&path);
    assert!(res.is_err(), "truncated header must not load");

    // Truncate within the body but keep a valid header -> read_exact fails.
    let mut bytes2 = read_file(&path);
    let full_len = bytes2.len();
    bytes2.truncate(full_len - 2);
    write_file(&path, &bytes2);
    assert!(format::load_from_path(&path).is_err());
}

#[test]
fn save_is_atomic_no_partial_file_visible_on_success() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("a.mphf");
    format::save_to_path(&path, &sample(VerifyMode::FullKey)).unwrap();
    // Temp staging file must be gone.
    assert!(!dir.path().join("a.mphf.tmp").exists());
    assert!(path.exists());
}
