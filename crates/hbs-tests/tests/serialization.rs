//! Format round-trips for every fixture and corruption rejection with the
//! precise failure category.

use hbs_core::HierBitmap;
use hbs_format::{ContainerKind, FormatError, HEADER_LEN, KIND_ARRAY, KIND_BITMAP, decode, encode};
use hbs_testkit::generator::{Distribution, sample};

fn build(values: &[u32]) -> HierBitmap {
    let mut v = values.to_vec();
    v.sort_unstable();
    v.dedup();
    HierBitmap::from_sorted_unique(&v).unwrap()
}

/// Recompute the checksum after tampering, so the decoder runs past the
/// checksum and reaches the specific validator being probed.
fn refresh_crc(bytes: &mut [u8]) {
    let mut c = hbs_format::crc32::Crc32::new();
    c.update(&bytes[..24]);
    c.update(&bytes[HEADER_LEN..]);
    bytes[24..28].copy_from_slice(&c.finish().to_le_bytes());
}

#[test]
fn roundtrip_every_fixture_distribution() {
    for d in [
        Distribution::Sparse,
        Distribution::Dense,
        Distribution::Interleaved,
        Distribution::ContainerThreshold,
    ] {
        for seed in [1u64, 42, 777] {
            let f = sample(d, seed);
            let set = build(&f.values);
            let bytes = encode(&set);
            let decoded =
                decode(&bytes).unwrap_or_else(|e| panic!("{} failed to round-trip: {e}", f.label));
            assert_eq!(decoded.set, set, "{}", f.label);
            assert_eq!(decoded.set.len(), f.values.len() as u64, "{}", f.label);
            assert_eq!(decoded.encoded_len, bytes.len(), "{}", f.label);
        }
    }
}

#[test]
fn roundtrip_preserves_container_kind_at_threshold() {
    let f = sample(Distribution::ContainerThreshold, 1);
    let set = build(&f.values);
    let decoded = decode(&encode(&set)).unwrap();
    assert_eq!(decoded.array_containers, 2);
    assert_eq!(decoded.bitmap_containers, 1);

    // Decode and inspect kinds directly from the file bytes.
    let bytes = encode(&set);
    let kinds: Vec<ContainerKind> = (0..3)
        .map(|i| {
            let base = HEADER_LEN + i * 16;
            match u16::from_le_bytes([bytes[base + 2], bytes[base + 3]]) {
                KIND_ARRAY => ContainerKind::Array,
                KIND_BITMAP => ContainerKind::Bitmap,
                _ => panic!("bad kind"),
            }
        })
        .collect();
    assert_eq!(
        kinds,
        vec![
            ContainerKind::Array,
            ContainerKind::Array,
            ContainerKind::Bitmap
        ]
    );
}

#[test]
fn corruption_flip_payload_byte_is_checksum_mismatch() {
    let f = sample(Distribution::Interleaved, 1);
    let mut bytes = encode(&build(&f.values));
    let target = bytes.len() / 2;
    bytes[target] ^= 0x80;
    let err = decode(&bytes).unwrap_err();
    assert!(
        matches!(err, FormatError::ChecksumMismatch { .. }),
        "expected checksum mismatch, got {err:?}"
    );
}

#[test]
fn corruption_truncation_is_truncated() {
    let f = sample(Distribution::Dense, 1);
    let bytes = encode(&build(&f.values));
    // Cutting exactly at the header must fail; cutting one payload byte too.
    assert_eq!(decode(&bytes[..HEADER_LEN]), Err(FormatError::Truncated));
    assert_eq!(
        decode(&bytes[..bytes.len() - 1]),
        Err(FormatError::Truncated)
    );
    assert_eq!(decode(&[]), Err(FormatError::Truncated));
}

#[test]
fn corruption_bad_magic() {
    let mut bytes = encode(&build(&[1u32, 2]));
    bytes[0] = b'X';
    assert_eq!(decode(&bytes).unwrap_err(), FormatError::BadMagic);
}

#[test]
fn corruption_bad_version_after_crc_refresh() {
    let mut bytes = encode(&build(&[1u32, 2]));
    bytes[4..6].copy_from_slice(&2u16.to_le_bytes());
    refresh_crc(&mut bytes);
    assert_eq!(
        decode(&bytes).unwrap_err(),
        FormatError::UnsupportedVersion { found: 2 }
    );
}

#[test]
fn corruption_set_flags_after_crc_refresh() {
    let mut bytes = encode(&build(&[1u32]));
    bytes[6..8].copy_from_slice(&0x8000u16.to_le_bytes());
    refresh_crc(&mut bytes);
    assert_eq!(decode(&bytes).unwrap_err(), FormatError::BadFlags(0x8000));
}

#[test]
fn corruption_unknown_container_kind() {
    let mut bytes = encode(&build(&[1u32]));
    bytes[HEADER_LEN + 2..HEADER_LEN + 4].copy_from_slice(&7u16.to_le_bytes());
    refresh_crc(&mut bytes);
    assert_eq!(
        decode(&bytes).unwrap_err(),
        FormatError::UnknownContainerKind(7)
    );
}

#[test]
fn corruption_array_cardinality_above_threshold() {
    let mut bytes = encode(&build(&[1u32, 2, 3]));
    bytes[HEADER_LEN + 4..HEADER_LEN + 8].copy_from_slice(&9000u32.to_le_bytes());
    refresh_crc(&mut bytes);
    let err = decode(&bytes).unwrap_err();
    assert!(
        matches!(err, FormatError::Cardinality { chunk: 0, .. }),
        "got {err:?}"
    );
}

#[test]
fn corruption_bitmap_cardinality_too_low() {
    // A dense chunk is required; the interleaved fixture has one.
    let f = sample(Distribution::Interleaved, 1);
    let mut bytes = encode(&build(&f.values));
    // Find the first bitmap entry and lie its cardinality down to 10.
    let n = u32::from_le_bytes(bytes[8..12].try_into().unwrap());
    let mut tampered = false;
    for i in 0..n {
        let base = HEADER_LEN + i as usize * 16;
        let kind = u16::from_le_bytes([bytes[base + 2], bytes[base + 3]]);
        if kind == KIND_BITMAP {
            bytes[base + 4..base + 8].copy_from_slice(&10u32.to_le_bytes());
            tampered = true;
            break;
        }
    }
    assert!(tampered, "fixture must contain a bitmap container");
    refresh_crc(&mut bytes);
    let err = decode(&bytes).unwrap_err();
    assert!(
        matches!(err, FormatError::Cardinality { .. }),
        "got {err:?}"
    );
}

#[test]
fn corruption_reordered_directory_keys() {
    let f = sample(Distribution::Interleaved, 1);
    let set = build(&f.values);
    let mut bytes = encode(&set);
    let n = u32::from_le_bytes(bytes[8..12].try_into().unwrap());
    assert!(n >= 2);
    // Copy entry 0's key over entry 1's key -> duplicate, not strictly inc.
    let first_key = [bytes[HEADER_LEN], bytes[HEADER_LEN + 1]];
    bytes[HEADER_LEN + 16..HEADER_LEN + 18].copy_from_slice(&first_key);
    refresh_crc(&mut bytes);
    let err = decode(&bytes).unwrap_err();
    assert!(matches!(err, FormatError::BadDirectory(_)), "got {err:?}");
}

#[test]
fn corruption_trailing_bytes() {
    let mut bytes = encode(&build(&[1u32]));
    bytes.push(0xAA);
    assert_eq!(decode(&bytes).unwrap_err(), FormatError::TrailingBytes);
}

#[test]
fn corruption_offset_gap_detected() {
    // Shift the first entry's declared data offset by 2 (still within the
    // data section): the strict back-to-back invariant must fire.
    let mut set_a = HierBitmap::new();
    set_a.insert(1);
    let mut set_b = HierBitmap::new();
    set_b.insert(200_000);
    let mut bytes = encode(&set_a.union(&set_b));
    // second entry offset lives at HEADER_LEN+16+8
    bytes[HEADER_LEN + 24..HEADER_LEN + 28].copy_from_slice(&4u32.to_le_bytes());
    refresh_crc(&mut bytes);
    let err = decode(&bytes).unwrap_err();
    assert!(matches!(err, FormatError::BadDirectory(_)), "got {err:?}");
}
