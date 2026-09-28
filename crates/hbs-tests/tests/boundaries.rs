//! Boundary semantics the contract calls out explicitly:
//! u32 universe edges, no integer overflow at the maximum value, and
//! configuration failure categories.

use hbs_config::{Config, ConfigError};
use hbs_core::HierBitmap;

#[test]
fn universe_endpoints_are_representable() {
    let mut s = HierBitmap::new();
    s.insert(0);
    s.insert(u32::MAX);
    assert_eq!(s.len(), 2);
    assert_eq!(s.min(), Some(0));
    assert_eq!(s.max(), Some(u32::MAX));

    // rank semantics at the last representable integer: no u32+1 needed.
    assert_eq!(s.rank_le(u32::MAX), 2);
    assert_eq!(s.rank_lt(u32::MAX), 1);
    assert_eq!(s.rank_le(0), 1);
    assert_eq!(s.rank_lt(0), 0);

    // select accepts u64 ranks and cannot overflow at the boundary.
    assert_eq!(s.select(0), Some(0));
    assert_eq!(s.select(1), Some(u32::MAX));
    assert_eq!(s.select(u32::MAX as u64), None);
    assert_eq!(s.select(u64::MAX), None);
}

#[test]
fn full_universe_cardinality_uses_u64_count() {
    // Build a set with two fully dense chunks (131,072 values) to exercise
    // counts larger than any single chunk, without constructing all 2^32.
    let mut s = HierBitmap::new();
    for v in 0..(1u32 << 17) {
        s.insert(v);
    }
    assert_eq!(s.len(), 1u64 << 17);
    assert_eq!(s.rank_lt(1u32 << 17), 1u64 << 17);
    assert_eq!(s.max(), Some((1u32 << 17) - 1));
}

#[test]
fn chunk_key_split_at_boundaries() {
    use hbs_core::set::{join, split};
    for v in [0u32, 1, 65535, 65536, 0xFFFF_FFFE, u32::MAX] {
        let (k, lo) = split(v);
        assert_eq!(join(k, lo), v);
    }
    let (k, lo) = split(u32::MAX);
    assert_eq!(k, u16::MAX);
    assert_eq!(lo, u16::MAX);
}

#[test]
fn config_specific_failure_categories() {
    let mut c = Config::default();
    assert!(matches!(
        c.apply_file("bogus = 1"),
        Err(ConfigError::UnknownKey(_))
    ));
    assert!(matches!(
        c.apply_file("max_request_bytes = 0"),
        Err(ConfigError::BadValue { .. })
    ));
    assert!(matches!(
        c.apply_file("bind_addr"),
        Err(ConfigError::Parse { .. })
    ));

    c.apply_file("log_level = \"debug\"").unwrap();
    assert_eq!(c.log_level, "debug");
}

#[test]
fn empty_set_serialises_and_reports_zero() {
    let s = HierBitmap::new();
    let bytes = hbs_format::encode(&s);
    let d = hbs_format::decode(&bytes).unwrap();
    assert_eq!(d.num_chunks, 0);
    assert!(d.set.is_empty());
    assert_eq!(d.set.len(), 0);
    assert_eq!(d.set.select(0), None);
}
