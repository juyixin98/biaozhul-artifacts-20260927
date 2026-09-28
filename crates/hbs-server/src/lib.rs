//! `hbs-server`: the Axum verification HTTP interface.
//!
//! Every response carries the request id (client-supplied `X-Request-Id` or
//! a generated UUID), the format version the server was built against, and
//! a trace of the key processing steps. Failures are reported separately from
//! uncertain conclusions: a malformed request is an error, while a verified
//! "not equal / checksum failed" verdict is a successful response with an
//! explicit, explainable result.

pub mod app;
pub mod model;

pub use app::{AppState, build_app};
