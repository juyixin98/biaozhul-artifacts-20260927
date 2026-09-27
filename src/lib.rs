//! Multi-module Reed-Solomon erasure-coding service.
//!
//! Module responsibilities:
//! - [`gf256`]: fixed GF(2^8) field arithmetic + matrix inversion (auditable).
//! - [`erasure`]: coding kernel — systematic Vandermonde encoding and
//!   erasure reconstruction.
//! - [`manifest`]: on-disk data format and authenticated digest covering
//!   original length, padding, parameters and shard checksums.
//! - [`storage`]: filesystem persistence adapter and shard auditing
//!   (missing vs corrupt distinction).
//! - [`service`]: orchestration of encode / retrieve / inspect / repair,
//!   enforcing "never return unverifiable data".
//! - [`api`]: Axum HTTP interface with per-request correlation ids.
//! - [`config`] / [`error`]: configuration and the explainable error model.

pub mod api;
pub mod config;
pub mod erasure;
pub mod error;
pub mod gf256;
pub mod manifest;
pub mod service;
pub mod storage;

pub use config::Config;
pub use service::AppState;
pub use storage::FileStore;

/// Construct the application state from configuration (ensures the field
/// tables and data directory are initialized).
pub async fn build_state(config: &Config) -> error::AppResult<AppState> {
    gf256::init();
    let store = FileStore::new(&config.data_dir).await?;
    Ok(AppState {
        store,
        allowed_profiles: config.allowed_profiles.clone(),
        max_object_bytes: config.max_object_bytes,
    })
}
