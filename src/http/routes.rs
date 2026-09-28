//! Router assembly and server bootstrap.

use std::net::SocketAddr;

use axum::extract::DefaultBodyLimit;
use axum::routing::{get, post};
use axum::Router;

use super::handlers::{self, AppState};
use super::MAX_BODY;
use crate::store::BlockStore;

/// Build the application router over an existing store-backed state.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(handlers::healthz))
        .route("/v1/streams", post(handlers::create_stream))
        .route("/v1/streams/{id}/blocks", post(handlers::append_block))
        .route(
            "/v1/streams/{id}/encode",
            post(handlers::encode_block_handler),
        )
        .route("/v1/streams/{id}/decode", get(handlers::decode_stream))
        .route("/v1/encode-independent", post(handlers::encode_independent))
        .route("/v1/decode", post(handlers::decode_one))
        .layer(DefaultBodyLimit::max(MAX_BODY))
        .with_state(state)
}

/// Build state from a store directory path.
pub fn state_for(root: &std::path::Path) -> crate::core::error::Result<AppState> {
    let store = BlockStore::open(root)?;
    Ok(AppState::new(store))
}

/// Run the server until shutdown.
pub async fn serve(addr: SocketAddr, state: AppState) -> Result<(), std::io::Error> {
    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!(%addr, store_root = %state.store.root().display(), "LZ77B listening");
    axum::serve(listener, router(state))
        .with_graceful_shutdown(shutdown_signal())
        .await
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    tracing::info!("shutdown requested");
}
