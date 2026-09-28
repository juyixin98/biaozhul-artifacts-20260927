//! Wavelet matrix service for integer sequences.
//!
//! Module responsibilities:
//! * [`bitvector`] — rank-supporting bit vector (kernel primitive)
//! * [`wavelet_matrix`] — wavelet matrix over u64 ranks (index kernel)
//! * [`compress`] — order-preserving coordinate compression
//! * [`index`] — i64 sequence index: k-th smallest, range counting,
//!   predecessor/successor with explicit error categories
//! * [`format`] — versioned binary format with payload checksum
//! * [`store`] — filesystem persistence adapter
//! * [`config`] — configuration parsing
//! * [`api`] — Axum validation interface

pub mod api;
pub mod bitvector;
pub mod compress;
pub mod config;
pub mod error;
pub mod format;
pub mod index;
pub mod store;
pub mod wavelet_matrix;
