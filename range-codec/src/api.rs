//! HTTP verification API (Axum).
//!
//! Endpoints (JSON unless noted; raw bytes accepted via
//! `Content-Type: application/octet-stream`):
//!
//! | method | path                     | purpose                              |
//! |--------|--------------------------|--------------------------------------|
//! | POST   | `/v1/verify`             | validate+decode, return verdict      |
//! | POST   | `/v1/encode/static`      | encode static chunks                 |
//! | POST   | `/v1/encode/adaptive`    | encode adaptive chunks               |
//! | PUT    | `/v1/artifacts/:id`      | store raw container bytes            |
//! | GET    | `/v1/artifacts/:id`      | fetch stored bytes + metadata        |
//! | POST   | `/v1/artifacts/:id/decode` | decode a stored artifact          |
//! | DELETE | `/v1/artifacts/:id`      | remove an artifact                   |
//! | GET    | `/healthz`               | liveness                             |
//!
//! Every response carries a `request_id` (honours an inbound `X-Request-Id`
//! header) and, on failure, `decision` (`rejected`/`indeterminate`),
//! `error.code` and `state`. Payloads are never echoed back.

use crate::config::Config;
use crate::container::{
    decode, encode_adaptive, encode_empty, encode_static, ModelMode, StaticChunk,
};
use crate::diagnostics::{log_diagnostic, Diagnostic, RequestId};
use crate::error::{CodecError, Result};
use crate::persist::Store;
use axum::{
    body::{to_bytes, Bytes},
    extract::{Path, State},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post, put},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub store: Arc<Store>,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/verify", post(verify))
        .route("/v1/encode/static", post(encode_static_handler))
        .route("/v1/encode/adaptive", post(encode_adaptive_handler))
        .route("/v1/artifacts/{id}", put(put_artifact).get(get_artifact).delete(delete_artifact))
        .route("/v1/artifacts/{id}/decode", post(decode_artifact))
        .with_state(state)
}

async fn healthz() -> &'static str {
    "ok\n"
}

// ---------------------------------------------------------------- helpers

fn request_id(headers: &HeaderMap) -> RequestId {
    headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .map(|s| RequestId(s.to_string()))
        .unwrap_or_default()
}

/// Error response body.
#[derive(Debug, Serialize)]
struct ErrorBody {
    request_id: String,
    decision: String,
    error: ErrorDetail,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    state: Vec<(String, String)>,
}

#[derive(Debug, Serialize)]
struct ErrorDetail {
    code: String,
    message: String,
}

impl AppState {
    fn fail(&self, rid: &RequestId, raw: &[u8], err: CodecError) -> Response {
        let d = Diagnostic::from_error(rid, raw, &err, vec![]);
        log_diagnostic(&d, raw, self.config.redact_payloads);
        let status = StatusCode::from_u16(err.http_status()).unwrap_or(StatusCode::UNPROCESSABLE_ENTITY);
        let body = ErrorBody {
            request_id: rid.0.clone(),
            decision: d.decision.clone(),
            error: ErrorDetail {
                code: err.code().into(),
                message: err.to_string(),
            },
            state: d.state.clone(),
        };
        (status, Json(body)).into_response()
    }

    fn ok<T: Serialize>(&self, rid: &RequestId, raw: &[u8], value: T) -> Response {
        let result: std::result::Result<(), CodecError> = Ok(());
        let d = Diagnostic::for_result(rid, raw, &result, vec![]);
        log_diagnostic(&d, raw, self.config.redact_payloads);
        Json(value).into_response()
    }
}

/// Read the whole body enforcing the configured limit with an exact error.
async fn read_body(headers: &HeaderMap, body: axum::body::Body, limit: usize) -> Result<Bytes> {
    let len_hint = headers
        .get(header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<usize>().ok());
    if let Some(len) = len_hint {
        if len > limit {
            return Err(CodecError::PayloadTooLarge {
                size: len,
                limit,
            });
        }
    }
    to_bytes(body, limit)
        .await
        .map_err(|_| CodecError::PayloadTooLarge {
            size: len_hint.unwrap_or(limit + 1),
            limit,
        })
}

fn wants_json(headers: &HeaderMap) -> bool {
    headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|ct| ct.contains("application/json"))
}

// ---------------------------------------------------------------- verify

#[derive(Debug, Deserialize)]
struct VerifyRequest {
    /// Base64 (standard, padded or not) container bytes.
    data: String,
}

#[derive(Debug, Serialize)]
struct VerifyResponse {
    request_id: String,
    decision: &'static str,
    chunks: usize,
    mode: &'static str,
    num_symbols: u16,
    decoded_len: usize,
    /// Base64 decoded symbols (bytes 0..num_symbols).
    symbols_b64: String,
}

fn b64_decode(s: &str) -> Result<Vec<u8>> {
    use base64ish::*;
    std_decode(s)
}

fn b64_encode(bytes: &[u8]) -> String {
    base64ish::std_encode(bytes)
}

async fn verify(
    State(st): State<AppState>,
    headers: HeaderMap,
    body: axum::body::Body,
) -> Response {
    let rid = request_id(&headers);
    let raw = match read_body(&headers, body, st.config.http_body_limit).await {
        Ok(b) => b,
        Err(e) => return st.fail(&rid, &[], e),
    };

    let container_bytes = if wants_json(&headers) {
        let parsed: VerifyRequest = match serde_json::from_slice(&raw) {
            Ok(v) => v,
            Err(e) => {
                return st.fail(&rid, &raw, CodecError::BadRequest(format!("invalid JSON: {e}")))
            }
        };
        match b64_decode(&parsed.data) {
            Ok(v) => v,
            Err(e) => return st.fail(&rid, &raw, e),
        }
    } else {
        raw.to_vec()
    };

    match decode(&container_bytes, &st.config.budget) {
        Ok(d) => {
            let resp = VerifyResponse {
                request_id: rid.0.clone(),
                decision: "accepted",
                chunks: d.chunks,
                mode: match d.mode {
                    ModelMode::Static => "static",
                    ModelMode::Adaptive => "adaptive",
                },
                num_symbols: d.num_symbols,
                decoded_len: d.symbols.len(),
                symbols_b64: b64_encode(&d.symbols),
            };
            st.ok(&rid, &container_bytes, resp)
        }
        Err(e) => st.fail(&rid, &container_bytes, e),
    }
}

// ---------------------------------------------------------------- encode

#[derive(Debug, Deserialize)]
struct StaticEncodeRequest {
    num_symbols: u16,
    /// Each item: { freqs: [u32], symbols_b64: string }.
    chunks: Vec<StaticChunkReq>,
}

#[derive(Debug, Deserialize)]
struct StaticChunkReq {
    freqs: Vec<u32>,
    symbols_b64: String,
}

#[derive(Debug, Serialize)]
struct EncodeResponse {
    request_id: String,
    data_b64: String,
    bytes: usize,
}

async fn encode_static_handler(
    State(st): State<AppState>,
    headers: HeaderMap,
    body: axum::body::Body,
) -> Response {
    let rid = request_id(&headers);
    let raw = match read_body(&headers, body, st.config.http_body_limit).await {
        Ok(b) => b,
        Err(e) => return st.fail(&rid, &[], e),
    };
    let req: StaticEncodeRequest = match serde_json::from_slice(&raw) {
        Ok(v) => v,
        Err(e) => return st.fail(&rid, &raw, CodecError::BadRequest(format!("invalid JSON: {e}"))),
    };

    let build = || -> Result<(Vec<StaticChunk>, Vec<u8>)> {
        if req.num_symbols == 0 || u32::from(req.num_symbols) > 256 {
            return Err(CodecError::BadAlphabetSize {
                size: req.num_symbols as usize,
                max: 256,
            });
        }
        let mut chunks = Vec::new();
        for c in &req.chunks {
            if c.freqs.len() != req.num_symbols as usize {
                return Err(CodecError::BadAlphabetSize {
                    size: c.freqs.len(),
                    max: 256,
                });
            }
            chunks.push(StaticChunk {
                freqs: c.freqs.clone(),
                symbols: b64_decode(&c.symbols_b64)?,
            });
        }
        let bytes = if chunks.is_empty() {
            encode_empty(ModelMode::Static, req.num_symbols)?
        } else {
            encode_static(&chunks)?
        };
        Ok((chunks, bytes))
    };

    match build() {
        Ok((_, bytes)) => st.ok(
            &rid,
            &bytes,
            EncodeResponse {
                request_id: rid.0.clone(),
                bytes: bytes.len(),
                data_b64: b64_encode(&bytes),
            },
        ),
        Err(e) => st.fail(&rid, &raw, e),
    }
}

#[derive(Debug, Deserialize)]
struct AdaptiveEncodeRequest {
    num_symbols: u16,
    /// Each item is base64 symbols for one chunk.
    chunks: Vec<String>,
}

async fn encode_adaptive_handler(
    State(st): State<AppState>,
    headers: HeaderMap,
    body: axum::body::Body,
) -> Response {
    let rid = request_id(&headers);
    let raw = match read_body(&headers, body, st.config.http_body_limit).await {
        Ok(b) => b,
        Err(e) => return st.fail(&rid, &[], e),
    };
    let req: AdaptiveEncodeRequest = match serde_json::from_slice(&raw) {
        Ok(v) => v,
        Err(e) => return st.fail(&rid, &raw, CodecError::BadRequest(format!("invalid JSON: {e}"))),
    };

    let build = || -> Result<Vec<u8>> {
        let mut chunks = Vec::new();
        for c in &req.chunks {
            chunks.push(b64_decode(c)?);
        }
        if chunks.is_empty() {
            encode_empty(ModelMode::Adaptive, req.num_symbols)
        } else {
            encode_adaptive(req.num_symbols, &chunks)
        }
    };

    match build() {
        Ok(bytes) => st.ok(
            &rid,
            &bytes,
            EncodeResponse {
                request_id: rid.0.clone(),
                bytes: bytes.len(),
                data_b64: b64_encode(&bytes),
            },
        ),
        Err(e) => st.fail(&rid, &raw, e),
    }
}

// ---------------------------------------------------------------- artifacts

#[derive(Debug, Serialize)]
struct ArtifactResponse {
    request_id: String,
    metadata: serde_json::Value,
    data_b64: String,
}

async fn put_artifact(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    body: axum::body::Body,
) -> Response {
    let rid = request_id(&headers);
    let raw = match read_body(&headers, body, st.config.http_body_limit).await {
        Ok(b) => b,
        Err(e) => return st.fail(&rid, &[], e),
    };
    // Peek mode from the flags byte (offset 5) after minimal magic check.
    let mode = if raw.len() > 5 && raw[5] & 0x01 == 0 {
        ModelMode::Static
    } else {
        ModelMode::Adaptive
    };
    match st.store.put(&id, mode, &raw) {
        Ok(meta) => st.ok(&rid, &raw, serde_json::to_value(meta).unwrap()),
        Err(e) => st.fail(&rid, &raw, e),
    }
}

async fn get_artifact(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let rid = request_id(&headers);
    match st.store.get(&id) {
        Ok(art) => st.ok(
            &rid,
            &art.data,
            ArtifactResponse {
                request_id: rid.0.clone(),
                metadata: serde_json::to_value(&art.meta).unwrap(),
                data_b64: b64_encode(&art.data),
            },
        ),
        Err(e) => st.fail(&rid, &[], e),
    }
}

async fn delete_artifact(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let rid = request_id(&headers);
    match st.store.delete(&id) {
        Ok(()) => st.ok(&rid, &[], serde_json::json!({"deleted": id, "request_id": rid.0})),
        Err(e) => st.fail(&rid, &[], e),
    }
}

async fn decode_artifact(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let rid = request_id(&headers);
    match st.store.decode(&id, &st.config.budget) {
        Ok(d) => st.ok(
            &rid,
            &[],
            serde_json::json!({
                "request_id": rid.0,
                "chunks": d.chunks,
                "mode": match d.mode {
                    ModelMode::Static => "static",
                    ModelMode::Adaptive => "adaptive",
                },
                "num_symbols": d.num_symbols,
                "decoded_len": d.symbols.len(),
                "symbols_b64": b64_encode(&d.symbols),
            }),
        ),
        Err(e) => st.fail(&rid, &[], e),
    }
}

// Minimal dependency-free base64 so the API needs no extra crate.
mod base64ish {
    use crate::error::{CodecError, Result};

    const TBL: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

    pub fn std_encode(input: &[u8]) -> String {
        let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
        for chunk in input.chunks(3) {
            let b0 = chunk[0] as u32;
            let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
            let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
            let triple = (b0 << 16) | (b1 << 8) | b2;
            out.push(TBL[((triple >> 18) & 63) as usize] as char);
            out.push(TBL[((triple >> 12) & 63) as usize] as char);
            if chunk.len() > 1 {
                out.push(TBL[((triple >> 6) & 63) as usize] as char);
            } else {
                out.push('=');
            }
            if chunk.len() > 2 {
                out.push(TBL[(triple & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
        out
    }

    fn val(c: u8) -> Result<u8> {
        match c {
            b'A'..=b'Z' => Ok(c - b'A'),
            b'a'..=b'z' => Ok(c - b'a' + 26),
            b'0'..=b'9' => Ok(c - b'0' + 52),
            b'+' => Ok(62),
            b'/' => Ok(63),
            _ => Err(CodecError::BadRequest(format!(
                "invalid base64 character {c:#04x}"
            ))),
        }
    }

    /// Accept standard padding; whitespace is rejected.
    pub fn std_decode(s: &str) -> Result<Vec<u8>> {
        let bytes = s.as_bytes();
        let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
        let mut quad = Vec::with_capacity(4);
        let mut pad = 0;
        for &c in bytes {
            if c == b'=' {
                pad += 1;
                quad.push(0);
            } else {
                if pad > 0 {
                    return Err(CodecError::BadRequest(
                        "padding must be terminal".into(),
                    ));
                }
                quad.push(val(c)?);
            }
            if quad.len() == 4 {
                let triple = ((quad[0] as u32) << 18)
                    | ((quad[1] as u32) << 12)
                    | ((quad[2] as u32) << 6)
                    | quad[3] as u32;
                out.push((triple >> 16) as u8);
                if pad < 2 {
                    out.push((triple >> 8) as u8);
                }
                if pad < 1 {
                    out.push(triple as u8);
                }
                quad.clear();
                if pad > 0 {
                    // No more groups after padding starts.
                    break;
                }
            }
        }
        if !quad.is_empty() {
            return Err(CodecError::BadRequest(
                "base64 input length not a multiple of 4".into(),
            ));
        }
        Ok(out)
    }
}
