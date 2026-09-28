//! `hbs-core`: the indexing kernel.
//!
//! A hierarchical (Roaring-style) set of `u32` values. The universe
//! `[0, 2^32)` is split into 65,536 chunks ("high containers"), each holding
//! the low 16 bits of its values. A chunk is stored in one of two physical
//! containers chosen by a fixed cardinality threshold:
//!
//! * [`Array`]  — a sorted, deduplicated `Vec<u16>` ("sparse"), used while
//!   cardinality `<= THRESHOLD`;
//! * [`Bitmap`] — 1,024 packed 64-bit words ("dense"), used once cardinality
//!   exceeds the threshold.
//!
//! No operation in this crate materialises the represented integer set as a
//! `Vec<u32>`: set algebra operates per chunk on the compressed containers,
//! and only an explicit iterator yields values, lazily.
pub mod container;
pub mod error;
pub mod iter;
pub mod select;
pub mod set;

pub use container::{Array, Bitmap, Container, THRESHOLD};
pub use error::CoreError;
pub use set::HierBitmap;

/// Number of values a single chunk covers (2^16).
pub const CHUNK_SIZE: u64 = 1 << 16;
/// Number of chunks covering the full `u32` universe (2^16).
pub const CHUNK_COUNT: usize = 1 << 16;
