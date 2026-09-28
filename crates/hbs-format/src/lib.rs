//! `hbs-format`: the on-disk / on-wire serialisation format.
//!
//! # File layout (version 1, little-endian)
//!
//! ```text
//! header (fixed 24 bytes)
//!   magic      4 bytes = b"HBS1"
//!   version    u16     = 1
//!   flags      u16     = 0 (reserved; any set bit is rejected)
//!   num_chunks u32
//!   dir_offset u32     = HEADER_LEN
//!   dir_len    u32     = num_chunks * 16
//!   data_len   u32
//!   checksum   u32     = CRC32 of every preceding byte of the file
//! directory (dir_len bytes), entries sorted by chunk key (16 bytes each)
//!   key        u16
//!   kind       u16     = 1 (array) | 2 (bitmap)
//!   cardinality u32
//!   data_offset u32    relative to data region start
//!   reserved   u32     = 0 (any set bit is rejected)
//! payload (data_len bytes), one region per directory entry
//!   array:  cardinality * u16, strictly increasing
//!   bitmap: 1024 * u64 = 8192 bytes, popcount == cardinality
//! ```
//!
//! Every invariant is checked on decode: magic/version/flags, the checksum,
//! directory order and bounds, the container kind, its cardinality (both the
//! declared value and actual array/bitmap content) and that offsets/lengths
//! fit inside the payload without overlap gaps being materialised. A
//! truncated file or tampered byte is rejected with a specific
//! [`FormatError`] variant.
pub mod crc32;
pub mod error;
pub mod format;

pub use error::FormatError;
pub use format::{
    ContainerKind, DecodedFile, FORMAT_MAGIC, FORMAT_VERSION, HEADER_LEN, KIND_ARRAY, KIND_BITMAP,
    decode, decode_from, encode, encode_to,
};
