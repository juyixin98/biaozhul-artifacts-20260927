//! range-codec: fixed-precision range encoder/decoder with static frequency
//! tables, adaptive integer-rescaling models and a chunked container.
//!
//! Crate layout (real, separated responsibilities):
//!
//! * [`model`] — data format of frequency tables, static/adaptive models and
//!   the integer rescale rule.
//! * [`range`] — the coding kernel (normalisation, carry propagation, tail).
//! * [`crc`] — checksum primitive.
//! * [`container`] — chunked `RC01` wire format, budgets and validation.
//! * [`persist`] — filesystem persistence adapter (atomic writes).
//! * [`api`] — Axum HTTP verification interface.
//! * [`diagnostics`] — request/record ids, key-state and redaction.
//! * [`config`] — file/env configuration.

pub mod config;
pub mod container;
pub mod crc;
pub mod diagnostics;
pub mod error;
pub mod model;
pub mod persist;
pub mod range;

pub mod api;

pub use error::{CodecError, Decision, Result};
