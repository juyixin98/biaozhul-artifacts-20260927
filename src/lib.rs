//! # lz77-blocks
//!
//! LZ77 sliding-window block compression backend.
//!
//! Module boundaries (the data/error contract each layer exposes):
//!
//! * [`error`] — shared [`error::CodecError`] + stable [`error::ErrorCategory`] taxonomy;
//! * [`format`] — wire format, fixed constants, envelope encode/parse, CRC-32, digest;
//! * [`lz77`] — encoding/index kernel and the byte-at-a-time overlap-correct decoder;
//! * [`codec`] — block envelope binding (independent vs dependent) and chain decoding;
//! * [`store`] — filesystem persistence adapter with a JSON manifest and chain links;
//! * [`service`] — Axum validation/operation HTTP interface.

pub mod codec;
pub mod error;
pub mod format;
pub mod lz77;
pub mod service;
pub mod store;

pub use error::{CodecError, ErrorCategory, Result};
