//! HTTP backend: Axum router, DTOs, shared state, categorized errors.

pub mod dto;
pub mod handlers;
pub mod state;

use std::sync::Arc;

pub use handlers::router;
pub use state::{ApiError, ApiResult, AppState};

/// Build the application with fresh state from resolved config.
pub fn app(state: Arc<AppState>) -> axum::Router {
    router(state)
}
