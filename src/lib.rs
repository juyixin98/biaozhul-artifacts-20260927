//! btmon — bounded temporal rule monitor over finite traces.
//!
//! Crate layout:
//! - [`lang`]: input language (events, conditions, response/sustain rules),
//!   three-valued verdicts and reason codes.
//! - [`error`]: the four-category typed error contract.
//! - [`canonical`]: canonical-JSON digest helpers for the evidence chain.
//! - [`monitor`]: the incremental monitoring kernel (state + decision log).
//! - [`reference`]: an independent set-based oracle used to cross-check the
//!   kernel; it never reads kernel internals.
//! - [`store`]/[`api`]: in-memory repository and Axum HTTP transport.

pub mod api;
pub mod canonical;
pub mod error;
pub mod lang;
pub mod monitor;
pub mod reference;
pub mod store;
