//! Persisted-format tests: round-trip and each concrete corruption class.

use mph_service::format::{self, FormatError, ALGORITHM_VERSION, FORMAT_VERSION};
use mph_service::kernel::builder::build;

fn sample_index() -> mph_service::kernel::MphIndex {
    let keys: Vec<Vec<u8>> = (0..20).map(|i| format!("k-{i}").into_bytes()).collect();
    build(&keys, 31337, 64, 1_000_000).unwrap().index
}

#[test]
fn roundtrip_preserves_seed_and_lookup_behaviour() {
    let idx = sample_index();
    let bytes = format::encode(&idx);
    assert_eq!(&bytes[0..8], b"MPHFBDZ\x01");
    assert_eq!(FORMAT_VERSION, 1);
    assert_eq!(ALGORITHM_VERSION, 1);

    let reloaded = format::decode(&bytes).unwrap();
    assert_eq!(reloaded.seed, idx.seed);
    assert_eq!(reloaded.n, idx.n);
    assert_eq!(reloaded.m, idx.m);
    assert_eq!(reloaded.g, idx.g);
    assert_eq!(reloaded.fps, idx.fps);

    for i in 0..20 {
        let key = format!("k-{i}");
        assert_eq!(
            format!("{:?}", reloaded.lookup(key.as_bytes())),
            format!("{:?}", idx.lookup(key.as_bytes())),
        );
    }
    // Non-member rejection survives reload.
    assert!(reloaded.lookup(b"k-999").ne(&mph_service::kernel::Lookup::Member {
        slot: 0
    }));
}

#[test]
fn truncated_file_is_classified_truncated() {
    let err = format::decode(&[1u8, 2, 3]).unwrap_err();
    match err {
        FormatError::Truncated { need, have } => {
            assert_eq!(need, format::HEADER_LEN);
            assert_eq!(have, 3);
        }
        other => panic!("expected Truncated, got {other:?}"),
    }
}

#[test]
fn bad_magic_is_classified() {
    let mut bytes = format::encode(&sample_index());
    bytes[0] = b'X';
    assert_eq!(format::decode(&bytes).unwrap_err(), FormatError::BadMagic);
}

#[test]
fn unsupported_format_version_is_classified() {
    let mut bytes = format::encode(&sample_index());
    bytes[8] = 99;
    bytes[9] = 0;
    match format::decode(&bytes).unwrap_err() {
        FormatError::UnsupportedFormatVersion(v) => assert_eq!(v, 99),
        other => panic!("expected UnsupportedFormatVersion, got {other:?}"),
    }
}

#[test]
fn unsupported_algorithm_version_is_classified() {
    let mut bytes = format::encode(&sample_index());
    bytes[10] = 7;
    bytes[11] = 0;
    match format::decode(&bytes).unwrap_err() {
        FormatError::UnsupportedAlgorithmVersion(v) => assert_eq!(v, 7),
        other => panic!("expected UnsupportedAlgorithmVersion, got {other:?}"),
    }
}

#[test]
fn tampered_payload_is_caught_by_crc() {
    let bytes = format::encode(&sample_index());
    let mut tampered = bytes.clone();
    let pos = format::HEADER_LEN + 4; // inside g[]
    tampered[pos] ^= 0xFF;
    assert_eq!(
        format::decode(&tampered).unwrap_err(),
        FormatError::CrcMismatch
    );
    // The seed is inside the CRC-covered header prefix, so tampering
    // with it is also rejected: the format binds the seed.
    let mut tampered_seed = bytes.clone();
    tampered_seed[16] ^= 0x01;
    assert_eq!(
        format::decode(&tampered_seed).unwrap_err(),
        FormatError::CrcMismatch
    );
}

#[test]
fn length_mismatch_is_classified() {
    let mut bytes = format::encode(&sample_index());
    bytes.push(0xFF);
    match format::decode(&bytes).unwrap_err() {
        FormatError::LengthMismatch { declared, actual } => {
            assert_eq!(declared + 1, actual);
        }
        other => panic!("expected LengthMismatch, got {other:?}"),
    }
}

#[test]
fn empty_index_roundtrips() {
    let idx = mph_service::kernel::MphIndex::empty(42);
    let bytes = format::encode(&idx);
    let reloaded = format::decode(&bytes).unwrap();
    assert_eq!(reloaded.n, 0);
    assert_eq!(reloaded.seed, 42);
}
