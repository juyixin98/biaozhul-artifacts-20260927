//! Versioned, deterministic object manifest format.
//!
//! # What a manifest holds
//!
//! Everything needed to locate and safely reconstruct one encoded object:
//! coding parameters `(k, m)`, shard byte length, original (pre-padding)
//! length and the number of padding bytes, the field/matrix **format
//! version**, the SHA-256 algorithm tag, and every shard's position-aware
//! digest (see [`ec_core::verify::digest_shard`]).
//!
//! Integrity has two layers:
//! 1. **Each shard** is authenticated by its position-aware digest. A bad
//!    shard (flipped bits, truncation, or two valid shards swapped) is
//!    classified bad and handled as an erasure — it is never fed to the
//!    solver.
//! 2. **The manifest itself** is authenticated by [`Manifest::digest`] over a
//!    deterministic TLV encoding of exactly the fields the format defines as
//!    covered. That covers the original length and padding explicitly, per
//!    requirement: an attacker who flips `pad_len` cannot trick truncation
//!    into returning padding bytes as object data.
//!
//! # Canonical covered encoding (write it out by hand for audit)
//!
//! All integers big-endian. Fields appear in tag order (0..=6) exactly once.
//!
//! ```text
//!  tag 0  u8   FORMAT_VERSION (=1)
//!  tag 1  u8   field_primitive ("GF2P8-0x11B-G3")
//!  tag 2  u8   k
//!  tag 3  u8   m
//!  tag 4  u32  shard_len
//!  tag 5  u64  original_len
//!  tag 6  u64  pad_len
//!  tag 7  str  digest_algorithm ("SHA-256")
//!  tag 8  str  object_id
//!  tag 9  u16  shard_count
//!         then shard_count entries, each:
//!           u16 index, u32 digest_len(=32), digest bytes
//! ```
//!
//! Each field is emitted as `tag(u8) || value`; the string tag's value is
//! `u16 len || utf8 bytes`; the shard list is the concatenation of its
//! entries immediately after the count. Independent implementations in the
//! tests/ directory reconstruct these bytes without calling this module.

#![forbid(unsafe_code)]

pub mod manifest;

pub use manifest::{
    build_manifest_digest, covered_encoding, Manifest, ShardDigestEntry,
    FORMAT_VERSION, FIELD_PRIMITIVE,
};
