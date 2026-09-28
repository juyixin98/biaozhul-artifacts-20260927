//! # hcomp
//!
//! Canonical-Huffman compression codec backend with a block-directory
//! container (`HCMP`), filesystem persistence and an Axum verification API.
//!
//! Module layout mirrors the requested engineering structure:
//! * [`huffman`], [`canonical`], [`bits`], [`block`] — data format & kernels;
//! * [`container`] — block directory, offsets, checksums, version handling;
//! * [`crc32`] — integrity primitive;
//! * [`store`] — filesystem persistence adapter;
//! * [`config`] — configuration layer;
//! * [`api`], [`runid`] — HTTP verification interface and request identity.

pub mod api;
pub mod bits;
pub mod block;
pub mod canonical;
pub mod config;
pub mod container;
pub mod crc32;
pub mod error;
pub mod huffman;
pub mod runid;
pub mod store;

/// Library semantic version, surfaced over the API/docs.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
