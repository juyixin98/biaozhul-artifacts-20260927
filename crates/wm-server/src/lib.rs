//! `wm-server`: Axum verification service for wavelet matrix indexes.
//!
//! Crate layout:
//! * [`config`] – layered configuration (CLI > env > TOML > defaults)
//! * [`request_id`] – per-request identity correlation
//! * [`api_error`] – typed failure categories and HTTP status mapping
//! * [`respond`] – success envelopes and diagnostic trace JSON
//! * [`handlers`] – index administration and query endpoints
//! * [`app`] – router assembly

pub mod api_error;
pub mod app;
pub mod config;
pub mod handlers;
pub mod request_id;
pub mod respond;

pub use app::build_app;
pub use config::Config;
