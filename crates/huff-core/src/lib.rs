//! huff-core: canonical-Huffman compression format kernel.
//!
//! Layers (see FORMAT.md for the authoritative wire specification):
//!
//! * [`crc`]      — IEEE CRC-32 used by every integrity layer
//! * [`bits`]     — MSB-first bit reader / writer
//! * [`huff`]     — deterministic canonical Huffman code construction + decode table
//! * [`block`]    — one independently decodable block payload
//! * [`container`]— block directory + checksums + version envelope
//! * [`report`]   — multi-step validation report (never collapses errors into success)
//! * [`error`]    — typed error kinds with stable machine codes

pub mod bits;
pub mod block;
pub mod container;
pub mod crc;
pub mod error;
pub mod huff;
pub mod report;

/// Container format magic `"HUFF"`.
pub const MAGIC: &[u8; 4] = b"HUFF";
/// Only format version this software encodes or accepts.
pub const FORMAT_VERSION: u8 = 1;
/// Maximum canonical code length in bits (see FORMAT.md §4).
pub const MAX_CODE_LEN: u8 = 32;
/// Largest block input size the encoder is willing to create.
pub const MAX_BLOCK_SIZE: u32 = 1 << 20;
/// Default block input size used when the caller does not specify one.
pub const DEFAULT_BLOCK_SIZE: u32 = 1 << 16;
