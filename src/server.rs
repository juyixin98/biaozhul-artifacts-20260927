//! HTTP validation interface (Axum).
//!
//! Endpoints:
//! * `GET  /health`           — liveness + storage path
//! * `POST /v1/encode`        — body: `{data_base64, mode?, frequencies?, chunk_target?}`
//! * `POST /v1/decode`        — body: `{container_base64}`
//! * `GET  /v1/jobs`          — list persisted jobs
//! * `GET  /v1/jobs/:id`      — decode one persisted job
//!
//! Every encode/decode response embeds a `diagnostics` object with a request
//! id, the decision, a stable `error_kind` on rejection, key derived state,
//! and a redacted payload fingerprint.  Raw payloads are only returned when
//! `expose_data` is enabled in configuration.

use crate::config::Config;
use crate::diagnostics::new_request_id;
use crate::error::ContainerError;
use crate::ops::{self, EncodeMode};
use crate::storage::FileStore;
use crate::table::FreqTable;

use axum::{
    extract::{Path, State},
    http::{HeaderMap, HeaderName, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

#[derive(Clone)]
struct AppState {
    config: Arc<Config>,
    store: Arc<FileStore>,
}

/// Build the router (also used by integration tests via `tower::ServiceExt`).
pub fn router(config: Config, store: FileStore) -> Router {
    let state = AppState {
        config: Arc::new(config),
        store: Arc::new(store),
    };
    Router::new()
        .route("/health", get(health))
        .route("/v1/encode", post(encode))
        .route("/v1/decode", post(decode))
        .route("/v1/jobs", get(list_jobs))
        .route("/v1/jobs/:id", get(get_job))
        .with_state(state)
}

/// Run the server until shutdown.
pub async fn serve(config: Config) -> Result<(), Box<dyn std::error::Error>> {
    let store = FileStore::open(&config.storage_dir)?;
    let app = router(config.clone(), store);
    let listener = tokio::net::TcpListener::bind(&config.bind_addr).await?;
    eprintln!("rangecode listening on http://{}", config.bind_addr);
    axum::serve(listener, app).await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Base64 (standard alphabet), hand-rolled to avoid a network dependency.
// ---------------------------------------------------------------------------

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn b64_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [
            chunk.first().copied().unwrap_or(0),
            if chunk.len() > 1 { chunk[1] } else { 0 },
            if chunk.len() > 2 { chunk[2] } else { 0 },
        ];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(B64[((n >> 18) & 63) as usize] as char);
        out.push(B64[((n >> 12) & 63) as usize] as char);
        if chunk.len() > 1 {
            out.push(B64[((n >> 6) & 63) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(B64[(n & 63) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

pub fn b64_decode(s: &str) -> Result<Vec<u8>, String> {
    fn val(c: u8) -> Option<u8> {
        match c {
            b'A'..=b'Z' => Some(c - b'A'),
            b'a'..=b'z' => Some(c - b'a' + 26),
            b'0'..=b'9' => Some(c - b'0' + 52),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    }
    let bytes = s.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return Err("base64 length must be a multiple of 4".to_string());
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for chunk in bytes.chunks(4) {
        let mut buf = [0u8; 4];
        let mut pad = 0;
        for (i, &c) in chunk.iter().enumerate() {
            if c == b'=' {
                pad += 1;
                buf[i] = 0;
            } else {
                buf[i] = val(c).ok_or_else(|| format!("invalid base64 byte 0x{c:02x}"))?;
            }
        }
        if pad > 2 || (pad == 1 && chunk[3] != b'=') {
            return Err("misplaced base64 padding".to_string());
        }
        let n = ((buf[0] as u32) << 18)
            | ((buf[1] as u32) << 12)
            | ((buf[2] as u32) << 6)
            | buf[3] as u32;
        out.push((n >> 16) as u8);
        if pad < 2 {
            out.push((n >> 8) as u8);
        }
        if pad < 1 {
            out.push(n as u8);
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn health(State(s): State<AppState>) -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "status": "ok",
        "service": "rangecode",
        "storage_dir": s.config.storage_dir.display().to_string(),
        "frequency_bound": s.config.frequency_bound,
    }))
}

#[derive(Debug, Deserialize)]
struct EncodeRequest {
    data_base64: String,
    #[serde(default)]
    mode: Option<String>,
    /// Optional explicit 256-entry frequency table.
    #[serde(default)]
    frequencies: Option<Vec<u32>>,
    /// Optional job id to persist the container under.
    #[serde(default)]
    id: Option<String>,
    #[serde(default)]
    chunk_target: Option<u32>,
}

#[derive(Debug, Serialize)]
struct EncodeResponse {
    request_id: String,
    container_base64: String,
    container_bytes: usize,
    declared_symbols: u64,
    chunks: usize,
    table_epochs: usize,
    diagnostics: serde_json::Value,
}

async fn encode(
    State(s): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<EncodeRequest>,
) -> Result<Json<EncodeResponse>, ApiError> {
    let request_id = request_id_from_headers(&headers);
    let data = b64_decode(&req.data_base64).map_err(|e| {
        ApiError::bad_request(&request_id, "invalid_base64", e, req.data_base64.as_bytes())
    })?;

    let mode = match req.mode.as_deref() {
        None | Some("static") => EncodeMode::Static,
        Some("adaptive") => EncodeMode::Adaptive,
        Some(other) => {
            return Err(ApiError::bad_request(
                &request_id,
                "unknown_mode",
                format!("mode must be static|adaptive, got {other:?}"),
                &data,
            ))
        }
    };

    // Explicit static frequencies take a fully validated table path.
    let result = if let Some(freqs) = req.frequencies {
        if mode == EncodeMode::Adaptive {
            return Err(ApiError::bad_request(
                &request_id,
                "conflicting_options",
                "explicit frequencies require mode=static".to_string(),
                &data,
            ));
        }
        let table = match FreqTable::with_declared_length(&freqs, s.config.frequency_bound, 256) {
            Ok(t) => t,
            Err(e) => {
                return Err(ApiError::container(
                    &request_id,
                    ContainerError::BadTable(e),
                    &data,
                ))
            }
        };
        // Zero-frequency symbols cannot be encoded: check up front so the
        // failure category is precise.
        if let Some(&b) = data.iter().find(|&&b| table.freq(b as usize) == 0) {
            return Err(ApiError::container(
                &request_id,
                ContainerError::BadStream(crate::error::DecodeError::CodePointOutsideEnvelope {
                    cum: b as u32,
                    total: table.total(),
                }),
                &data,
            ));
        }
        crate::container::encode_static(&data, &table, 256).map(|blob| ops::EncodeResult {
            container: blob,
            declared_symbols: data.len() as u64,
            chunks: if data.is_empty() { 0 } else { 1 },
            table_epochs: 1,
        })
    } else {
        let (r, _) = ops::encode_bytes(
            &request_id,
            &data,
            mode,
            s.config.frequency_bound,
            req.chunk_target.unwrap_or(s.config.chunk_target),
        );
        r
    };

    match result {
        Ok(er) => {
            if let Some(id) = &req.id {
                let meta = serde_json::json!({
                    "request_id": request_id,
                    "declared_symbols": er.declared_symbols,
                    "chunks": er.chunks,
                    "table_epochs": er.table_epochs,
                    "input_len": data.len(),
                });
                s.store
                    .put(id, &er.container, &meta)
                    .map_err(|e| ApiError::storage(&request_id, e, &data))?;
            }
            let diagnostics = crate::diagnostics::DiagnosticRecord::accepted(
                "encode",
                request_id.clone(),
                &data,
                serde_json::json!({
                    "mode": mode.as_str(),
                    "bound": s.config.frequency_bound,
                    "declared_symbols": er.declared_symbols,
                    "container_bytes": er.container.len(),
                    "chunks": er.chunks,
                    "table_epochs": er.table_epochs,
                    "persisted": req.id.is_some(),
                }),
                "accepted",
            );
            Ok(Json(EncodeResponse {
                container_base64: b64_encode(&er.container),
                container_bytes: er.container.len(),
                declared_symbols: er.declared_symbols,
                chunks: er.chunks,
                table_epochs: er.table_epochs,
                diagnostics: serde_json::to_value(diagnostics).unwrap(),
                request_id,
            }))
        }
        Err(e) => Err(ApiError::container(&request_id, e, &data)),
    }
}

#[derive(Debug, Deserialize)]
struct DecodeRequest {
    container_base64: String,
}

async fn decode(
    State(s): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<DecodeRequest>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let request_id = request_id_from_headers(&headers);
    let container = b64_decode(&req.container_base64).map_err(|e| {
        ApiError::bad_request(
            &request_id,
            "invalid_base64",
            e,
            req.container_base64.as_bytes(),
        )
    })?;
    let budgets = s.config.budgets();
    let (result, record) = ops::decode_bytes(&request_id, &container, &budgets);
    match result {
        Ok(dr) => {
            let mut body = serde_json::json!({
                "request_id": request_id,
                "declared_symbols": dr.declared_symbols,
                "chunks": dr.chunks,
                "table_epochs": dr.table_epochs,
                "diagnostics": serde_json::to_value(&record).unwrap(),
            });
            if s.config.expose_data {
                body["data_base64"] = serde_json::Value::String(b64_encode(&dr.data));
                body["data_len"] = serde_json::json!(dr.data.len());
            } else {
                body["data_len"] = serde_json::json!(dr.data.len());
            }
            Ok(Json(body))
        }
        Err(e) => Err(ApiError::container(&request_id, e, &container)),
    }
}

async fn list_jobs(State(s): State<AppState>) -> Result<Json<serde_json::Value>, ApiError> {
    let ids = s.store.list().map_err(ApiError::storage_simple)?;
    Ok(Json(serde_json::json!({ "jobs": ids })))
}

async fn get_job(
    State(s): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let request_id = request_id_from_headers(&headers);
    let container = s
        .store
        .get(&id)
        .map_err(|e| ApiError::storage(&request_id, e, &[]))?;
    let budgets = s.config.budgets();
    let (result, record) = ops::decode_bytes(&request_id, &container, &budgets);
    match result {
        Ok(dr) => {
            let mut body = serde_json::json!({
                "request_id": request_id,
                "id": id,
                "declared_symbols": dr.declared_symbols,
                "chunks": dr.chunks,
                "table_epochs": dr.table_epochs,
                "diagnostics": serde_json::to_value(&record).unwrap(),
            });
            if s.config.expose_data {
                body["data_base64"] = serde_json::Value::String(b64_encode(&dr.data));
            }
            Ok(Json(body))
        }
        Err(e) => Err(ApiError::container(&request_id, e, &container)),
    }
}

// ---------------------------------------------------------------------------
// Error mapping
// ---------------------------------------------------------------------------

struct ApiError {
    status: StatusCode,
    request_id: String,
    body: serde_json::Value,
}

impl ApiError {
    fn bad_request(request_id: &str, kind: &'static str, message: String, input: &[u8]) -> Self {
        let rec = crate::diagnostics::DiagnosticRecord::rejected(
            "request",
            request_id,
            input,
            serde_json::json!({}),
            kind,
            message.clone(),
        );
        Self {
            status: StatusCode::BAD_REQUEST,
            request_id: request_id.to_string(),
            body: serde_json::json!({
                "request_id": request_id,
                "error_kind": kind,
                "message": message,
                "diagnostics": serde_json::to_value(rec).unwrap(),
            }),
        }
    }

    fn container(request_id: &str, e: ContainerError, input: &[u8]) -> Self {
        use crate::diagnostics::container_error_kind;
        let kind = container_error_kind(&e);
        let rec = crate::diagnostics::DiagnosticRecord::rejected(
            "request",
            request_id,
            input,
            serde_json::json!({"status": "rejected"}),
            kind,
            e.to_string(),
        );
        Self {
            status: StatusCode::UNPROCESSABLE_ENTITY,
            request_id: request_id.to_string(),
            body: serde_json::json!({
                "request_id": request_id,
                "error_kind": kind,
                "message": e.to_string(),
                "diagnostics": serde_json::to_value(rec).unwrap(),
            }),
        }
    }

    fn storage(request_id: &str, e: crate::storage::StorageError, input: &[u8]) -> Self {
        let rec = crate::diagnostics::DiagnosticRecord::indeterminate(
            "storage",
            request_id,
            input,
            serde_json::json!({}),
            e.to_string(),
        );
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            request_id: request_id.to_string(),
            body: serde_json::json!({
                "request_id": request_id,
                "error_kind": "storage_io",
                "message": e.to_string(),
                "diagnostics": serde_json::to_value(rec).unwrap(),
            }),
        }
    }

    fn storage_simple(e: crate::storage::StorageError) -> ApiError {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            request_id: String::new(),
            body: serde_json::json!({
                "error_kind": "storage_io",
                "message": e.to_string(),
            }),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let mut resp = (self.status, Json(self.body)).into_response();
        if let Ok(v) = HeaderValue::from_str(&self.request_id) {
            resp.headers_mut()
                .insert(HeaderName::from_static("x-request-id"), v);
        }
        resp
    }
}

fn request_id_from_headers(headers: &HeaderMap) -> String {
    headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .unwrap_or_else(new_request_id)
}
