//! Router assembly: routes, limits, request-id middleware and fallbacks.

use std::sync::Arc;
use std::time::Instant;

use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::middleware as axum_mw;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::json;

use wm_format::FORMAT_VERSION;
use wm_store::IndexStore;

use crate::handlers::{self, AppState};
use crate::request_id::{layer as request_id_layer, RequestId};
use crate::respond;

const MAX_BODY_BYTES: usize = 16 * 1024 * 1024;

/// Shared, cloneable context (also used by integration tests).
#[derive(Clone)]
pub struct AppContext {
    pub state: AppState,
    pub started_at: Arc<Instant>,
    pub data_dir: String,
}

/// Build the application against a store directory.
pub fn build_app(data_dir: &std::path::Path) -> Result<Router, wm_store::StoreError> {
    let store = Arc::new(IndexStore::open(data_dir)?);
    let state = AppState { store };

    let router = Router::new()
        .route("/", get(service_info))
        .route("/health", get(health))
        .route("/indexes", get(handlers::list_indexes))
        .route("/indexes/{name}", post(handlers::create_named_index))
        .route(
            "/indexes/{name}",
            get(handlers::get_index).delete(handlers::delete_index),
        )
        .route("/query", post(handlers::query))
        .fallback(fallback)
        .layer(axum::extract::DefaultBodyLimit::max(MAX_BODY_BYTES))
        .layer(axum_mw::from_fn(request_id_layer))
        .with_state(state);
    Ok(router)
}

/// Spawn the server on an ephemeral local port and return its address.
/// Used by integration tests.
pub async fn serve_local(router: Router) -> std::io::Result<std::net::SocketAddr> {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr()?;
    tokio::spawn(async move {
        let _ = axum::serve(listener, router).await;
    });
    Ok(addr)
}

async fn service_info(axum::Extension(id): axum::Extension<RequestId>) -> impl IntoResponse {
    let data = json!({
        "service": "wavelet-matrix-verifier",
        "version": env!("CARGO_PKG_VERSION"),
        "format_version": FORMAT_VERSION,
        "semantics": {
            "index_range": "half-open [l, r), requires l < r",
            "k": "0-based order statistic: 0 <= k < r-l",
            "value_range": "half-open [lo, hi)",
            "values": "i64; coordinate compression is order preserving"
        },
        "endpoints": {
            "GET /": "this document",
            "GET /health": "liveness and metadata",
            "GET /indexes": "list persisted indexes (corrupt entries listed separately)",
            "POST /indexes/{name}": "build and persist an index: {values:[i64...], overwrite:bool}",
            "GET /indexes/{name}": "index metadata",
            "DELETE /indexes/{name}": "remove an index",
            "POST /query": "tagged body {op} in {quantile,count,predecessor,successor}"
        }
    });
    Json(respond::ok(id.as_str(), data, None))
}

async fn health(
    State(state): State<AppState>,
    axum::Extension(id): axum::Extension<RequestId>,
) -> impl IntoResponse {
    let listing = state.store.list();
    let (count, corrupt, store_ok) = match listing {
        Ok(l) => (l.indexes.len(), l.corrupt.len(), true),
        Err(_) => (0usize, 0usize, false),
    };
    let status = if store_ok { "ok" } else { "degraded" };
    let data = json!({
        "status": status,
        "service": "wavelet-matrix-verifier",
        "version": env!("CARGO_PKG_VERSION"),
        "format_version": FORMAT_VERSION,
        "data_dir": state.store.path().display().to_string(),
        "index_count": count,
        "corrupt_entries": corrupt
    });
    let code = if store_ok {
        StatusCode::OK
    } else {
        StatusCode::INTERNAL_SERVER_ERROR
    };
    (code, Json(respond::ok(id.as_str(), data, None)))
}

/// JSON 404 for unknown routes, still request-id correlated.
async fn fallback(
    axum::Extension(id): axum::Extension<RequestId>,
    req: Request,
) -> impl IntoResponse {
    let body = json!({
        "ok": false,
        "request_id": id.as_str(),
        "error": {
            "kind": "ROUTE_NOT_FOUND",
            "message": format!("no route for {} {}", req.method(), req.uri()),
        }
    });
    (StatusCode::NOT_FOUND, Json(body))
}
