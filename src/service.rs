//! Axum HTTP adapter: the validation interface and the block service.
//!
//! ## Routes
//!
//! | method | path                       | purpose                                      |
//! |--------|----------------------------|----------------------------------------------|
//! | GET    | `/health`                  | liveness + store stats                       |
//! | GET    | `/`                        | service description                          |
//! | POST   | `/blocks`                  | compress+store; body `{mode,data,prev_id?}`  |
//! | GET    | `/blocks`                  | list block metadata                          |
//! | GET    | `/blocks/:id`              | block metadata                               |
//! | GET    | `/blocks/:id/frame`        | raw frame bytes (`application/octet-stream`) |
//! | GET    | `/blocks/:id/raw`          | decompress one block                         |
//! | GET    | `/chain/raw`               | decompress whole chain (tip) concatenated    |
//! | POST   | `/validate`                | parse/bind/decode a frame without storing    |
//!
//! `data` fields are standard base64. Error bodies are
//! `{"error":{"category":"…","detail":"…"}}` and the category is one of the stable
//! strings defined in [`crate::error::ErrorCategory`], so clients can branch on
//! `input_error` vs `state_conflict` vs `resource_exhausted` programmatically.

use axum::{
    body::Body,
    extract::{Path, Query, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::codec;
use crate::error::{CodecError, ErrorCategory};
use crate::format::dictionary_digest;
use crate::store::{BlockMeta, BlockStore};

#[derive(Clone)]
pub struct AppState {
    pub store: BlockStore,
}

pub fn app(store: BlockStore) -> Router {
    let state = AppState { store };
    Router::new()
        .route("/", get(index))
        .route("/health", get(health))
        .route("/blocks", get(list_blocks).post(create_block))
        .route("/blocks/{id}", get(get_meta))
        .route("/blocks/{id}/frame", get(get_frame))
        .route("/blocks/{id}/raw", get(get_raw))
        .route("/chain/raw", get(get_chain_raw))
        .route("/validate", post(validate_frame))
        .with_state(state)
}

// --------------------------------------------------------------------------- DTOs

#[derive(Debug, Deserialize)]
pub struct CreateBlockReq {
    /// "independent" | "dependent"
    pub mode: String,
    /// base64 payload bytes
    pub data: String,
    /// optional optimistic pin for dependent blocks
    #[serde(default)]
    pub prev_id: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct BlockMetaJson {
    pub id: String,
    pub mode: String,
    pub prev_id: Option<String>,
    pub sequence: u64,
    pub data_len: u64,
    pub payload_len: u64,
    pub created_at_unix_ms: u128,
}

impl From<BlockMeta> for BlockMetaJson {
    fn from(m: BlockMeta) -> Self {
        Self {
            id: m.id,
            mode: m.mode,
            prev_id: m.prev_id,
            sequence: m.sequence,
            data_len: m.data_len,
            payload_len: m.payload_len,
            created_at_unix_ms: m.created_at_unix_ms,
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct ValidateReq {
    /// base64 raw frame bytes
    pub frame: String,
    /// mode to bind against
    pub mode: String,
    /// for dependent validation: base64 predecessor dictionary
    #[serde(default)]
    pub dictionary: Option<String>,
}

// --------------------------------------------------------------------------- handlers

async fn index() -> Json<serde_json::Value> {
    Json(json!({
        "service": "lz77-blocks",
        "version": env!("CARGO_PKG_VERSION"),
        "window_bytes": crate::format::WINDOW_BYTES,
        "min_match": crate::format::MIN_MATCH,
        "max_match": crate::format::MAX_MATCH,
        "routes": [
            "GET  /health",
            "POST /blocks",
            "GET  /blocks",
            "GET  /blocks/:id",
            "GET  /blocks/:id/frame",
            "GET  /blocks/:id/raw",
            "GET  /chain/raw",
            "POST /validate",
        ],
    }))
}

async fn health(State(st): State<AppState>) -> Response {
    match st.store.list() {
        Ok(blocks) => Json(json!({
            "status": "ok",
            "blocks": blocks.len(),
            "tip": blocks.last().map(|b| b.id.clone()),
        }))
        .into_response(),
        Err(e) => map_err(e),
    }
}

async fn create_block(State(st): State<AppState>, raw_body: axum::body::Bytes) -> Response {
    let req: CreateBlockReq = match parse_json(&raw_body) {
        Ok(r) => r,
        Err(e) => return map_err(e),
    };
    let data = match B64.decode(req.data.as_bytes()) {
        Ok(d) => d,
        Err(e) => {
            return map_err(CodecError::input(format!("data is not valid base64: {e}")));
        }
    };

    let meta = match req.mode.as_str() {
        "independent" => st.store.put_independent(&data),
        "dependent" => {
            let pin = req.prev_id.as_deref();
            st.store.put_dependent(&data, pin)
        }
        other => Err(CodecError::input(format!(
            "unknown mode {other:?} (want \"independent\" or \"dependent\")"
        ))),
    };

    match meta {
        Ok(meta) => (StatusCode::CREATED, Json(BlockMetaJson::from(meta))).into_response(),
        Err(e) => map_err(e),
    }
}

async fn list_blocks(State(st): State<AppState>) -> Response {
    match st.store.list() {
        Ok(metas) => {
            let body: Vec<BlockMetaJson> = metas.into_iter().map(Into::into).collect();
            Json(body).into_response()
        }
        Err(e) => map_err(e),
    }
}

async fn get_meta(State(st): State<AppState>, Path(id): Path<String>) -> Response {
    match st.store.meta(&id) {
        Ok(meta) => Json(BlockMetaJson::from(meta)).into_response(),
        Err(e) => map_err(e),
    }
}

async fn get_frame(State(st): State<AppState>, Path(id): Path<String>) -> Response {
    match st.store.get_frame(&id) {
        Ok(frame) => Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "application/octet-stream")
            .header(
                header::CONTENT_DISPOSITION,
                format!("attachment; filename=\"{id}.lz71\""),
            )
            .body(Body::from(frame))
            .unwrap_or_else(|e| {
                map_err(CodecError::internal(format!("build frame response: {e}")))
            }),
        Err(e) => map_err(e),
    }
}

async fn get_raw(State(st): State<AppState>, Path(id): Path<String>) -> Response {
    match st.store.read_block(&id) {
        Ok(bytes) => raw_bytes_response(bytes, "decompressed block"),
        Err(e) => map_err(e),
    }
}

#[derive(Debug, Deserialize)]
struct TipQuery {
    #[serde(default)]
    tip: Option<String>,
}

async fn get_chain_raw(State(st): State<AppState>, Query(q): Query<TipQuery>) -> Response {
    match st.store.read_chain(q.tip.as_deref()) {
        Ok(bytes) => raw_bytes_response(bytes, "decompressed chain"),
        Err(e) => map_err(e),
    }
}

async fn validate_frame(State(_st): State<AppState>, raw_body: axum::body::Bytes) -> Response {
    let req: ValidateReq = match parse_json(&raw_body) {
        Ok(r) => r,
        Err(e) => return map_err(e),
    };
    let frame = match B64.decode(req.frame.as_bytes()) {
        Ok(f) => f,
        Err(e) => {
            return map_err(CodecError::input(format!("frame is not valid base64: {e}")));
        }
    };

    let dictionary = match (req.mode.as_str(), req.dictionary) {
        ("independent", None) | ("independent", Some(_)) => Vec::new(),
        ("dependent", Some(b64)) => match B64.decode(b64.as_bytes()) {
            Ok(d) => d,
            Err(e) => {
                return map_err(CodecError::input(format!(
                    "dictionary is not valid base64: {e}"
                )));
            }
        },
        ("dependent", None) => {
            return map_err(CodecError::input(
                "dependent validation requires a base64 \"dictionary\" field",
            ));
        }
        (other, _) => {
            return map_err(CodecError::input(format!(
                "unknown mode {other:?} (want \"independent\" or \"dependent\")"
            )));
        }
    };

    let report = codec::validate(&frame, &dictionary);
    // Even an invalid validate() call is a well-formed HTTP request; its status
    // encodes the SAME category contract as every other route (400/409/413/404).
    let status = if report.valid {
        StatusCode::OK
    } else {
        match report.error_category.as_deref() {
            Some(cat) if cat == ErrorCategory::StateConflict.as_ref() => StatusCode::CONFLICT,
            Some(cat) if cat == ErrorCategory::ResourceExhausted.as_ref() => {
                StatusCode::PAYLOAD_TOO_LARGE
            }
            Some(cat) if cat == ErrorCategory::NotFound.as_ref() => StatusCode::NOT_FOUND,
            _ => StatusCode::BAD_REQUEST,
        }
    };
    let body = json!({
        "valid": report.valid,
        "mode": report.mode.map(|m| match m {
            crate::format::BlockMode::Independent => "independent",
            crate::format::BlockMode::Dependent => "dependent",
        }),
        "data_len": report.data_len,
        "payload_len": report.payload_len,
        "crc32": report.crc32.map(|c| format!("{c:08x}")),
        "dict_digest": report.digest_hex,
        "error_category": report.error_category,
        "reason": report.reason,
        "dictionary_digest": hex_lower(&dictionary_digest(&dictionary)),
    });
    (status, Json(body)).into_response()
}

// --------------------------------------------------------------------------- helpers

fn raw_bytes_response(bytes: Vec<u8>, _what: &str) -> Response {
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "application/octet-stream")
        .body(Body::from(bytes))
        .unwrap_or_else(|e| map_err(CodecError::internal(format!("build raw response: {e}"))))
}

fn parse_json<T: serde::de::DeserializeOwned>(body: &[u8]) -> Result<T, CodecError> {
    serde_json::from_slice(body)
        .map_err(|e| CodecError::input(format!("request body is not valid JSON: {e}")))
}

fn hex_lower(bytes: &[u8]) -> String {
    codec::hex_encode(bytes)
}

fn map_err(e: CodecError) -> Response {
    let status = match e.category {
        ErrorCategory::Input => StatusCode::BAD_REQUEST,
        ErrorCategory::StateConflict => StatusCode::CONFLICT,
        ErrorCategory::ResourceExhausted => StatusCode::PAYLOAD_TOO_LARGE,
        ErrorCategory::NotFound => StatusCode::NOT_FOUND,
        ErrorCategory::ComputeFailure => StatusCode::INTERNAL_SERVER_ERROR,
    };
    let body = Json(json!({
        "error": {
            "category": e.category.as_ref(),
            "detail": e.detail,
        }
    }));
    (status, body).into_response()
}
