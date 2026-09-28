//! Persistence corruption tests.
//!
//! A valid index file is produced through the real write path, then mutated
//! at specific offsets. Every mutation must be rejected on reload with
//! category `compute_failure` and code `persistence_corrupt` (or
//! `catalog_corrupt`), never silently loaded, never panicked. Assertions pin
//! the precise offending section recorded in the message.

mod common;

use std::fs;
use std::path::Path;

use fm_index_svc::error::{Error, ErrorCategory};
use fm_index_svc::persistence::{Catalog, parse_index_bytes};

fn parse(dir: &Path, name: &str) -> Result<(), Error> {
    let cat = Catalog::open(dir).unwrap();
    cat.open_index(name).map(|_| ())
}

fn build_valid(dir: &Path, name: &str, text: &[u8], k: u32) {
    let mut cat = Catalog::open(dir).unwrap();
    cat.create(name, text.to_vec(), k).unwrap();
}

#[test]
fn valid_file_round_trips() {
    let dir = tempfile::tempdir().unwrap();
    build_valid(dir.path(), "ok", b"she sells sea shells", 3);
    parse(dir.path(), "ok").unwrap();
}

fn assert_corrupt(res: Result<(), Error>, section_contains: &str) {
    let err = res.expect_err("corruption must be rejected");
    assert_eq!(
        err.category(),
        ErrorCategory::ComputeFailure,
        "corruption is a compute failure, got {err:?}"
    );
    let msg = err.to_string();
    assert!(
        msg.contains(section_contains),
        "message {msg:?} should name section {section_contains:?}"
    );
    match err {
        Error::PersistenceCorrupt { section, detail } => {
            assert!(
                section.contains(section_contains),
                "section {section} should contain {section_contains}; detail: {detail}"
            );
        }
        other => panic!("expected PersistenceCorrupt, got {other:?}"),
    }
}

#[test]
fn flipped_magic_is_rejected() {
    let dir = tempfile::tempdir().unwrap();
    build_valid(dir.path(), "m", b"abcdefgh", 2);
    let path = dir.path().join("m.fm");
    let mut bytes = fs::read(&path).unwrap();
    bytes[0] ^= 0xFF;
    let err = parse_index_bytes(&bytes).unwrap_err();
    assert_corrupt(Err(err), "magic");
}

#[test]
fn truncation_at_each_layer_is_rejected() {
    let dir = tempfile::tempdir().unwrap();
    build_valid(dir.path(), "t", b"abcdefghijkl", 2);
    let good = fs::read(dir.path().join("t.fm")).unwrap();
    // Cut while still inside each layer: header / TXT / BWT / SAS.
    for cut in [8, good.len() - 1, good.len() - 9, 40] {
        let cut = cut.min(good.len());
        let err = parse_index_bytes(&good[..cut]).unwrap_err();
        assert!(
            matches!(err, Error::PersistenceCorrupt { .. }),
            "cut at {cut} must be a corruption error, got {err:?}"
        );
    }
}

#[test]
fn crc_flip_in_each_section_is_detected() {
    let dir = tempfile::tempdir().unwrap();
    build_valid(dir.path(), "c", b"abcdefghijklmnop".repeat(4).as_slice(), 4);
    let path = dir.path().join("c.fm");

    // Find each section tag and flip one payload byte after its 12-byte
    // header (tag + len + crc).
    for (tag, label) in [(b"TXT1", "TXT1"), (b"BWT1", "BWT1"), (b"SAS1", "SAS1")] {
        let mut bytes = fs::read(&path).unwrap();
        let off = find_tag(&bytes, tag).unwrap_or_else(|| panic!("tag {label}"));
        let payload_start = off + 12;
        bytes[payload_start] ^= 0x01;
        let err = parse_index_bytes(&bytes).unwrap_err();
        // CRC is verified before any semantic check.
        assert_corrupt(Err(err), label);
    }
}

#[test]
fn semantically_bad_bwt_symbol_is_rejected() {
    // Valid CRC but a BWT symbol outside the alphabet: must get past CRC and
    // fail the semantic check (and therefore not be "fixed" silently).
    let dir = tempfile::tempdir().unwrap();
    build_valid(dir.path(), "s", b"abcdef", 2);
    let path = dir.path().join("s.fm");
    let mut bytes = fs::read(&path).unwrap();
    let off = find_tag(&bytes, b"BWT1").unwrap();
    let payload_start = off + 12;
    // payload begins with u64 n; first symbol at +8. Rewrite it to 257.
    bytes[payload_start + 8..payload_start + 10].copy_from_slice(&257u16.to_le_bytes());
    fix_section_crc(&mut bytes, off);
    let err = parse_index_bytes(&bytes).unwrap_err();
    assert_corrupt(Err(err), "bwt");
}

#[test]
fn tampered_sa_sample_is_rejected_against_rebuild() {
    // A wrong SA sample passes all structural checks and must be caught by the
    // full SA rebuild cross-check on load.
    let dir = tempfile::tempdir().unwrap();
    build_valid(dir.path(), "sa", b"abracadabra-abracadabra", 3);
    let path = dir.path().join("sa.fm");
    let mut bytes = fs::read(&path).unwrap();
    let off = find_tag(&bytes, b"SAS1").unwrap();
    let payload_start = off + 12;
    // payload: u64 count then u32 samples; flip the second sample.
    bytes[payload_start + 8 + 4..payload_start + 8 + 8].copy_from_slice(&999_999u32.to_le_bytes());
    fix_section_crc(&mut bytes, off);
    let err = parse_index_bytes(&bytes).unwrap_err();
    assert_corrupt(Err(err), "sa_samples");
}

#[test]
fn sample_interval_mismatch_is_rejected() {
    let dir = tempfile::tempdir().unwrap();
    build_valid(dir.path(), "k", b"abcdefgh", 2);
    let path = dir.path().join("k.fm");
    let mut bytes = fs::read(&path).unwrap();
    // header: magic(8) + name_len(4) + name(1) then u32 interval
    let pos = 8 + 4 + 1;
    bytes[pos..pos + 4].copy_from_slice(&7u32.to_le_bytes());
    let err = parse_index_bytes(&bytes).unwrap_err();
    assert_corrupt(Err(err), "sa_samples"); // sample count no longer matches
}

#[test]
fn corrupted_catalog_json_is_catalog_corrupt() {
    let dir = tempfile::tempdir().unwrap();
    build_valid(dir.path(), "g", b"abc", 1);
    fs::write(dir.path().join("catalog.json"), "{ broken json").unwrap();
    let err = match Catalog::open(dir.path()) {
        Err(e) => e,
        Ok(_) => panic!("corrupt catalog must fail to open"),
    };
    assert!(matches!(err, Error::CatalogCorrupt(_)), "got {err:?}");
    assert_eq!(err.category(), ErrorCategory::ComputeFailure);
}

#[test]
fn catalog_registering_missing_file_surfaces_io_compute_failure() {
    let dir = tempfile::tempdir().unwrap();
    build_valid(dir.path(), "g", b"abc", 1);
    fs::remove_file(dir.path().join("g.fm")).unwrap();
    let err = parse(dir.path(), "g").unwrap_err();
    assert_eq!(err.category(), ErrorCategory::ComputeFailure);
    assert!(matches!(err, Error::Io(_)));
}

// --------------------------------------------------------------------------

fn find_tag(bytes: &[u8], tag: &[u8; 4]) -> Option<usize> {
    bytes.windows(4).position(|w| w == tag)
}

fn fix_section_crc(bytes: &mut [u8], section_off: usize) {
    let len =
        u32::from_le_bytes(bytes[section_off + 4..section_off + 8].try_into().unwrap()) as usize;
    let payload = &bytes[section_off + 12..section_off + 12 + len];
    let crc = crc32fast::hash(payload);
    bytes[section_off + 8..section_off + 12].copy_from_slice(&crc.to_le_bytes());
}
