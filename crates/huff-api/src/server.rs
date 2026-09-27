//! Axum HTTP service: encode / decode / validate / artifact endpoints.
//!
//! Bodies are raw bytes (`application/octet-stream`), so empty input is a
//! legal encode request rather than a rejected one. Every failure carries a
//! stable error code; unknown/invalid input is never answered as success.

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::Router;
use serde::Serialize;

use huff_core::container::{encode_container, parse_container};
use huff_core::error::HuffError;
use huff_core::report::{self, ValidationReport};
use huff_core::FORMAT_VERSION;
use huff_store::{Store, StoreError};

use crate::config::Config;
use crate::logging;

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Store>,
    pub block_size: u32,
    pub max_body: usize,
}

/// Build the router (shared by server and integration tests).
pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/encode", post(encode))
        .route("/v1/decode", post(decode))
        .route("/v1/validate", post(validate))
        .route("/v1/artifacts", get(list_artifacts))
        .route("/v1/artifacts/{id}", get(read_artifact))
        .with_state(state)
}

/// JSON body of error responses.
#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub ok: bool,
    pub error_code: String,
    pub message: String,
    pub request_id: String,
}

fn err(status: StatusCode, code: &str, message: String, request_id: &str) -> Response {
    let body = ErrorBody {
        ok: false,
        error_code: code.to_string(),
        message,
        request_id: request_id.to_string(),
    };
    let json = serde_json::to_vec(&body).unwrap_or_default();
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json")
        .body(axum::body::Body::from(json))
        .unwrap()
}

fn map_huff_error(e: HuffError, request_id: &str) -> Response {
    let status = match e {
        HuffError::BadMagic
        | HuffError::UnknownVersion
        | HuffError::UnknownFlags
        | HuffError::BadBlockSize => StatusCode::UNPROCESSABLE_ENTITY,
        _ => StatusCode::BAD_REQUEST,
    };
    err(status, e.code(), format!("{e}"), request_id)
}

fn map_store_error(e: StoreError, request_id: &str) -> Response {
    match e {
        StoreError::NotFound(id) => err(
            StatusCode::NOT_FOUND,
            "ARTIFACT_NOT_FOUND",
            format!("artifact not found: {id}"),
            request_id,
        ),
        StoreError::Invalid(code) => {
            err(StatusCode::UNPROCESSABLE_ENTITY, "INVALID_CONTAINER", code, request_id)
        }
        other => err(
            StatusCode::INTERNAL_SERVER_ERROR,
            "STORE_ERROR",
            other.to_string(),
            request_id,
        ),
    }
}

/// New request id: 16 hex chars from time + pid + monotonic counter.
pub fn request_id() -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let mix = (std::process::id() as u64)
        .wrapping_mul(0x9E37_79B9_7F4A_7C15)
        .wrapping_add(n)
        .rotate_left(17)
        ^ n.wrapping_mul(0xD1B5_4A92_B349_5C57);
    format!("{mix:016x}")
}

async fn healthz() -> impl IntoResponse {
    #[derive(Serialize)]
    struct Health {
        ok: bool,
        service: &'static str,
        format_version: u8,
    }
    axum::Json(Health { ok: true, service: "huff-canonical", format_version: FORMAT_VERSION })
}

fn label_from(headers: &HeaderMap) -> Option<String> {
    headers
        .get("x-label")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
}

async fn encode(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let rid = request_id();
    logging::record(
        &rid,
        &format!("encode: received {} input bytes", body.len()),
    );
    if body.len() > state.max_body {
        return err(
            StatusCode::PAYLOAD_TOO_LARGE,
            "BODY_TOO_LARGE",
            format!("input exceeds {} byte limit", state.max_body),
            &rid,
        );
    }
    let container = match encode_container(&body, state.block_size) {
        Ok(c) => c,
        Err(e) => return map_huff_error(e, &rid),
    };
    logging::record(
        &rid,
        &format!(
            "encode: encoded {} -> {} container bytes (format v{})",
            body.len(),
            container.len(),
            FORMAT_VERSION
        ),
    );
    let record = match state.store.put(&container, label_from(&headers)) {
        Ok(r) => r,
        Err(e) => return map_store_error(e, &rid),
    };
    logging::record(&rid, &format!("encode: stored artifact {}", record.id));

    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/octet-stream")
        .header("x-artifact-id", record.id)
        .header("x-format-version", FORMAT_VERSION.to_string())
        .body(axum::body::Body::from(container))
        .unwrap()
}

async fn decode(State(state): State<AppState>, body: Bytes) -> Response {
    let rid = request_id();
    logging::record(
        &rid,
        &format!("decode: received {} container bytes", body.len()),
    );
    if body.len() > state.max_body {
        return err(
            StatusCode::PAYLOAD_TOO_LARGE,
            "BODY_TOO_LARGE",
            format!("input exceeds {} byte limit", state.max_body),
            &rid,
        );
    }
    // Parse first so error categorisation is precise.
    if let Err(e) = parse_container(&body) {
        return map_huff_error(e, &rid);
    }
    match huff_core::report::decode(&body) {
        Ok(decoded) => {
            logging::record(&rid, &format!("decode: verified and decoded {} bytes", decoded.len()));
            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, "application/octet-stream")
                .header("x-original-length", decoded.len().to_string())
                .body(axum::body::Body::from(decoded))
                .unwrap()
        }
        Err(e) => map_huff_error(e, &rid),
    }
}

async fn validate(State(_state): State<AppState>, body: Bytes) -> Response {
    let rid = request_id();
    logging::record(
        &rid,
        &format!("validate: running multi-step checks over {} bytes", body.len()),
    );
    let report: ValidationReport = report::validate(&body);
    for check in &report.checks {
        logging::record(
            &rid,
            &format!(
                "validate: check {:<16} verdict={:<4} basis={}",
                check.name, check.verdict, check.detail
            ),
        );
    }
    logging::record(
        &rid,
        &format!(
            "validate: overall ok={} first_error={:?}",
            report.ok, report.first_error
        ),
    );
    let status = if report.ok {
        StatusCode::OK
    } else {
        StatusCode::UNPROCESSABLE_ENTITY
    };
    let json = serde_json::to_vec(&report).unwrap_or_default();
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json")
        .body(axum::body::Body::from(json))
        .unwrap()
}

async fn list_artifacts(State(state): State<AppState>) -> Response {
    let rid = request_id();
    match state.store.list() {
        Ok(records) => axum::Json(records).into_response(),
        Err(e) => map_store_error(e, &rid),
    }
}

async fn read_artifact(State(state): State<AppState>, Path(id): Path<String>) -> Response {
    let rid = request_id();
    let container = match state.store.get(&id) {
        Ok(c) => c,
        Err(e) => return map_store_error(e, &rid),
    };
    match huff_core::report::decode(&container) {
        Ok(decoded) => Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "application/octet-stream")
            .body(axum::body::Body::from(decoded))
            .unwrap(),
        Err(e) => map_huff_error(e, &rid),
    }
}

/// Bind the server.
pub async fn serve(config: Config) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let store = Store::open(&config.data_dir)?;
    let state = AppState {
        store: Arc::new(store),
        block_size: config.block_size,
        max_body: config.max_body,
    };
    let listener = tokio::net::TcpListener::bind(&config.bind).await?;
    tracing::info!(
        "huff-canonical listening on http://{} (data_dir={}, block_size={}, format v{})",
        config.bind,
        config.data_dir.display(),
        config.block_size,
        FORMAT_VERSION
    );
    axum::serve(listener, app(state)).await?;
    Ok(())
}

/// Bind an ephemeral in-process server for integration tests. The task is
/// detached and ends when the test runtime shuts down.
pub async fn spawn_ephemeral(
    data_dir: std::path::PathBuf,
    block_size: u32,
    max_body: usize,
) -> std::io::Result<std::net::SocketAddr> {
    let store = Store::open(&data_dir).expect("open test store");
    let state = AppState { store: Arc::new(store), block_size, max_body };
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await?;
    let addr = listener.local_addr()?;
    tokio::spawn(async move {
        let _ = axum::serve(listener, app(state)).await;
    });
    Ok(addr)
}
