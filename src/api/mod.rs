//! HTTP 验证接口层（Axum）。

pub mod dto;
pub mod extractor;
pub mod routes;

pub use routes::{router, AppState};
