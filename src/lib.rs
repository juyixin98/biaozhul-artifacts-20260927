//! # rangecode
//!
//! Fixed-precision range encoder/decoder with:
//!
//! * integer-only interval math and carry propagation ([`range`]),
//! * validated, bounded static frequency tables ([`table`]),
//! * bounded adaptive counts with atomic rescale points ([`rescale`]),
//! * a chunked, CRC-protected on-disk container ([`container`]),
//! * a filesystem persistence adapter ([`storage`]),
//! * structured, redacted diagnostics ([`diagnostics`]).
//!
//! The module [`ops`] ties the pieces together for the HTTP and CLI layers.

pub mod config;
pub mod container;
pub mod diagnostics;
pub mod error;
pub mod format;
pub mod range;
pub mod rescale;
pub mod storage;
pub mod table;

#[cfg(feature = "server")]
pub mod server;

pub mod ops;

pub use config::{Config, ConfigError};
pub use container::{decode_container, encode_adaptive, encode_static, Budgets, ParsedContainer};
pub use error::{ContainerError, DecodeError, EncodeError, TableError};
pub use range::{RangeDecoder, RangeEncoder};
pub use rescale::{AdaptiveModel, ObserveOutcome};
pub use storage::FileStore;
pub use table::{FreqTable, MAX_BOUND};
