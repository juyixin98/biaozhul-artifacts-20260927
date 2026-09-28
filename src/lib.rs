//! FM-index byte-text service: BWT + rank structure + position sampling,
//! exposed over Axum with a file-system persistence adapter.
//!
//! Module boundaries and data contracts:
//!
//! * [`coding`]      — the unique sentinel and the 257-symbol byte alphabet
//! * [`suffix`]      — suffix array (prefix doubling, O(n log n))
//! * [`bwt`]         — BWT and C-table derivation
//! * [`rank`]        — occurrence structure with block snapshots
//! * [`fm`]          — index core: LF, half-open backwards search, sampling
//! * [`reference`]   — independent exhaustive-scan oracle + text statistics
//! * [`persistence`] — binary file format, CRC/SHA validation, catalog
//! * [`service`]     — Axum routes, error categorization, request logging
//! * [`config`] / [`logging`] — startup config and replayable request records

pub mod bwt;
pub mod coding;
pub mod config;
pub mod error;
pub mod fm;
pub mod logging;
pub mod persistence;
pub mod rank;
pub mod reference;
pub mod service;
pub mod suffix;

use std::time::{SystemTime, UNIX_EPOCH};

pub fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}
