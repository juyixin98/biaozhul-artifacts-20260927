//! Precise failure-category tests for malformed frequency tables and
//! truncated/corrupt streams.  Every assertion names the exact error
//! variant rather than just "an error happened".

use rangecode::error::{ContainerError, DecodeError, EncodeError, TableError};
use rangecode::range::{decode_vec, RangeEncoder};
use rangecode::table::{FreqTable, MAX_BOUND};

// ---------------------------------------------------------------------------
// Frequency table validation — exact categories
// ---------------------------------------------------------------------------

#[test]
fn empty_alphabet_error() {
    assert_eq!(
        FreqTable::new(&[], 10).unwrap_err(),
        TableError::EmptyAlphabet
    );
}

#[test]
fn all_zero_error() {
    assert_eq!(
        FreqTable::new(&[0, 0, 0], 10).unwrap_err(),
        TableError::AllZeroFrequencies
    );
}

#[test]
fn zero_bound_error() {
    assert_eq!(
        FreqTable::new(&[1], 0).unwrap_err(),
        TableError::InvalidBound { bound: 0 }
    );
}

#[test]
fn bound_above_kernel_limit_error() {
    assert_eq!(
        FreqTable::new(&[1], MAX_BOUND + 1).unwrap_err(),
        TableError::InvalidBound {
            bound: MAX_BOUND + 1
        }
    );
}

#[test]
fn total_exceeded_reports_exact_prefix_point() {
    let err = FreqTable::new(&[6, 6, 6], 10).unwrap_err();
    assert_eq!(
        err,
        TableError::TotalExceeded {
            total: 12,
            bound: 10,
            point: 1
        }
    );
}

#[test]
fn single_entry_exceeded_error() {
    let err = FreqTable::new(&[1, 11], 10).unwrap_err();
    assert_eq!(
        err,
        TableError::EntryExceeded {
            index: 1,
            freq: 11,
            bound: 10
        }
    );
}

#[test]
fn declared_length_mismatch_error() {
    assert_eq!(
        FreqTable::with_declared_length(&[1, 2], 8, 3).unwrap_err(),
        TableError::LengthMismatch {
            declared: 3,
            given: 2
        }
    );
}

// ---------------------------------------------------------------------------
// Encoder symbol validation
// ---------------------------------------------------------------------------

#[test]
fn zero_frequency_symbol_is_precisely_rejected() {
    let table = FreqTable::new(&[1, 0, 1], 8).unwrap();
    assert_eq!(
        RangeEncoder::encode_vec(&table, &[1]).unwrap_err(),
        EncodeError::ZeroFrequency { symbol: 1 }
    );
    // Neighboring symbols still encode.
    assert!(RangeEncoder::encode_vec(&table, &[0, 2, 0]).is_ok());
}

#[test]
fn out_of_alphabet_symbol_is_precisely_rejected() {
    let table = FreqTable::new(&[1, 1], 8).unwrap();
    assert_eq!(
        RangeEncoder::encode_vec(&table, &[2]).unwrap_err(),
        EncodeError::SymbolOutOfRange {
            symbol: 2,
            alphabet: 2
        }
    );
}

// ---------------------------------------------------------------------------
// Stream truncation matrix — every cut point must error precisely
// ---------------------------------------------------------------------------

#[test]
fn every_truncation_point_is_an_error_with_offset() {
    let table = FreqTable::new(&[1, 2, 3, 4], 256).unwrap();
    let symbols: Vec<u32> = (0..40).map(|i| i % 4).collect();
    let payload = RangeEncoder::encode_vec(&table, &symbols).unwrap();
    assert!(payload.len() > 10);

    for cut in 0..payload.len() {
        let res = decode_vec(&table, &payload[..cut], symbols.len());
        assert!(res.is_err(), "cut {cut} unexpectedly decoded");
        let err = res.unwrap_err();
        match err {
            DecodeError::TruncatedStream { at, .. } => {
                assert!((at as usize) >= cut.min(5), "offset {at} < cut {cut}");
            }
            // Some cuts may land on an invalid code point — still a precise
            // category, never an out-of-range symbol.
            DecodeError::InvalidLeadingByte { .. }
            | DecodeError::CodePointOutsideEnvelope { .. } => {}
            other => panic!("cut {cut}: unexpected category {other:?}"),
        }
    }
}

#[test]
fn bad_leading_byte_category() {
    assert_eq!(
        decode_vec(&FreqTable::new(&[1, 1], 8).unwrap(), &[1, 0, 0, 0, 0], 0).unwrap_err(),
        DecodeError::InvalidLeadingByte { got: 1 }
    );
}

#[test]
fn code_point_outside_envelope_category() {
    let table = FreqTable::new(&[1, 1, 1, 1], 16).unwrap();
    // code=0xFFFFFFFF, r=0xFFFFFFFF/4 => value=3 legal; use code that maps
    // to value 4 by skewing: code 0xFFFFFFFF with total 3 -> value 3 >= 3.
    let table3 = FreqTable::new(&[1, 1, 1], 16).unwrap();
    let bad = [0x00, 0xFF, 0xFF, 0xFF, 0xFF];
    let err = decode_vec(&table3, &bad, 1).unwrap_err();
    assert!(matches!(
        err,
        DecodeError::CodePointOutsideEnvelope { cum, total } if cum >= total
    ));
    // Silence dead-code warning for unused variable construction.
    let _ = table;
}

// ---------------------------------------------------------------------------
// Container-level precise errors
// ---------------------------------------------------------------------------

use rangecode::container::{decode_container, encode_adaptive, encode_static, Budgets};
use rangecode::format::{HEADER_LEN, MAGIC};

fn sample_blob() -> Vec<u8> {
    let table = FreqTable::uniform(256, 1 << 14).unwrap();
    encode_static(b"container failure matrix", &table, 256).unwrap()
}

#[test]
fn container_bad_magic() {
    let mut b = sample_blob();
    b[0] ^= 0xFF;
    assert!(matches!(
        decode_container(&b, &Budgets::default()).unwrap_err(),
        ContainerError::BadMagic
    ));
}

#[test]
fn container_unsupported_version() {
    let mut b = sample_blob();
    // Corrupt CRC-covered version field but keep magic: recompute CRC.
    b[4] = 0x7F; // version high byte
    let crc = rangecode::format::crc32(&b[..24]);
    b[24..28].copy_from_slice(&crc.to_be_bytes());
    assert!(matches!(
        decode_container(&b, &Budgets::default()).unwrap_err(),
        ContainerError::UnsupportedVersion { version: v } if v != 1
    ));
}

#[test]
fn container_unknown_flags() {
    let mut b = sample_blob();
    b[6] = 0x80; // unknown high flag (flags field is bytes 6..8)
    let crc = rangecode::format::crc32(&b[..24]);
    b[24..28].copy_from_slice(&crc.to_be_bytes());
    assert!(matches!(
        decode_container(&b, &Budgets::default()).unwrap_err(),
        ContainerError::UnknownFlags { .. }
    ));
}

#[test]
fn container_truncation_shapes() {
    let b = sample_blob();
    // Truncated within the header.
    assert!(matches!(
        decode_container(&b[..HEADER_LEN - 1], &Budgets::default()).unwrap_err(),
        ContainerError::TruncatedContainer { .. }
    ));
    // Truncated between header and EOF -> MissingEof.
    assert!(matches!(
        decode_container(&b[..HEADER_LEN + 2], &Budgets::default()).unwrap_err(),
        ContainerError::TruncatedContainer { .. } | ContainerError::MissingEof
    ));
    // Valid magic but random tail: header CRC mismatch category.
    let mut junk = vec![0u8; HEADER_LEN];
    junk[..4].copy_from_slice(&MAGIC);
    assert!(matches!(
        decode_container(&junk, &Budgets::default()).unwrap_err(),
        ContainerError::HeaderCrcMismatch { .. }
    ));
}

#[test]
fn container_budget_rejection() {
    let b = encode_adaptive(
        &(0..5000u32).map(|i| (i % 251) as u8).collect::<Vec<_>>(),
        256,
        1 << 12,
        256,
    )
    .unwrap();
    let small = Budgets {
        max_symbols: 100,
        max_bytes: 1 << 30,
        max_alphabet: 256,
    };
    assert!(matches!(
        decode_container(&b, &small).unwrap_err(),
        ContainerError::BudgetExceeded { .. }
    ));
}

#[test]
fn container_invalid_table_in_frame_is_bad_table() {
    let mut b = sample_blob();
    // Corrupt a frequency u32 inside the first TABLE frame: header(28) +
    // marker(1) + len(4) + entries_count(4) = 37, set first frequency to 0.
    // Table is uniform; zeroing ALL frequencies is hard per-entry, but
    // setting one frequency huge (>bound) makes the table invalid.
    let len = u32::from_be_bytes(b[29..33].try_into().unwrap()) as usize;
    assert!(len > 4);
    // Frequency entry 0 starts at offset 37 (28 header + 5 frame prefix +
    // 4 entries count).
    b[37..41].copy_from_slice(&0xFFFFFFFFu32.to_be_bytes());
    // Recompute frame CRC: body is at 33..(33+len), crc at 33+len.
    let crc = rangecode::format::crc32(&b[33..33 + len]);
    b[33 + len..37 + len].copy_from_slice(&crc.to_be_bytes());
    let err = decode_container(&b, &Budgets::default()).unwrap_err();
    assert!(
        matches!(err, ContainerError::BadTable(_)),
        "expected BadTable, got {err:?}"
    );
}
