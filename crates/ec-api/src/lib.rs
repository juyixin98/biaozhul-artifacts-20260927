//! Service + HTTP layer for the Reed-Solomon erasure-coding service.
//!
//! Module responsibilities:
//! - [`config`] — deployment configuration (JSON file).
//! - [`service`] — application orchestration: verify, classify, recover,
//!   repair, shared by store-backed and stateless entry points.
//! - [`report`] — explainable result types with explicit failure and
//!   uncertainty lists.
//! - [`dto`] — wire-level request bodies.
//! - [`http`] — Axum router, request-id correlation and error envelope.

#![forbid(unsafe_code)]

pub mod config;
pub mod dto;
pub mod http;
pub mod report;
pub mod service;

pub use config::ServerConfig;
pub use http::router;
pub use service::{ErasureCodingService, InputShard};
