//! Request handlers and the error/HTTP-status contract.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};

use crate::core::decoder::{encode_next, ChainSession};
use crate::core::encoder::encode_block;
use crate::core::error::{Category, Code, Error};
use crate::core::format::{BlockHeader, FrameType};
use crate::reference;
use crate::store::BlockStore;
use crate::util_b64;

/// Shared application state.
#[derive(Clone)]
pub struct AppState {
    pub store: Arc<BlockStore>,
    /// Per-stream live codec sessions (dictionary caches).
    pub sessions: Arc<Mutex<BTreeMap<String, ChainSession>>>,
}

impl AppState {
    pub fn new(store: BlockStore) -> Self {
        AppState {
            store: Arc::new(store),
            sessions: Arc::new(Mutex::new(BTreeMap::new())),
        }
    }
}

/// Extract the run id: honour `X-Run-Id` if present/sane, else synthesize one.
fn run_id(headers: &HeaderMap) -> String {
    if let Some(v) = headers.get("x-run-id").and_then(|v| v.to_str().ok()) {
        let v = v.trim();
        if !v.is_empty() && v.len() <= 64 && v.bytes().all(|b| b.is_ascii_graphic()) {
            return v.to_string();
        }
    }
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("run-{nanos:x}")
}

/// Stable HTTP status mapping for the four error categories. The body always
/// carries the fine-grained `category`; statuses follow REST conventions, so a
/// missing resource within the `state` category maps to 404 rather than 409.
fn status_for(code: Code) -> StatusCode {
    match code {
        Code::NotFound => StatusCode::NOT_FOUND,
        _ => match code.category() {
            Category::Input => StatusCode::BAD_REQUEST,
            Category::State => StatusCode::CONFLICT,
            Category::Resource => StatusCode::PAYLOAD_TOO_LARGE,
            Category::Compute => StatusCode::INTERNAL_SERVER_ERROR,
        },
    }
}

/// Error envelope returned for every non-2xx response.
struct ApiError {
    status: StatusCode,
    body: Value,
}

impl ApiError {
    fn from_error(run: &str, e: Error) -> Self {
        let status = status_for(e.code);
        tracing::warn!(
            run_id = %run,
            category = %e.category(),
            code = %e.code_name(),
            detail = %e.detail,
            "request failed"
        );
        ApiError {
            status,
            body: json!({
                "ok": false,
                "run_id": run,
                "category": e.category().to_string(),
                "error_code": e.code_name(),
                "detail": e.detail,
            }),
        }
    }

    fn bad_request(run: &str, msg: impl Into<String>) -> Self {
        Self::from_error(run, Error::new(Code::BadRequest, msg))
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let run = self.body["run_id"].as_str().unwrap_or("").to_string();
        let mut resp = (self.status, Json(self.body)).into_response();
        attach_run_header(resp.headers_mut(), &run);
        resp
    }
}

fn attach_run_header(headers: &mut axum::http::HeaderMap, run: &str) {
    if let Ok(v) = HeaderValue::from_str(run) {
        headers.insert(HeaderName::from_static("x-run-id"), v);
    }
}

fn ok_json(run: &str, value: Value) -> Response {
    let body = json!({ "ok": true, "run_id": run });
    let mut merged = json!({});
    if let (Some(o1), Some(o2)) = (body.as_object(), value.as_object()) {
        let mut m = o2.clone();
        for (k, v) in o1 {
            m.insert(k.clone(), v.clone());
        }
        merged = Value::Object(m);
    }
    let mut resp = (StatusCode::OK, Json(merged)).into_response();
    attach_run_header(resp.headers_mut(), run);
    resp
}

/// Parse a JSON body, treating serde failures as [`Code::BadRequest`].
fn json_value(bytes: &Bytes, run: &str) -> Result<Value, ApiError> {
    serde_json::from_slice::<Value>(bytes)
        .map_err(|e| ApiError::bad_request(run, format!("invalid JSON body: {e}")))
}

fn b64_field(value: &Value, key: &str, run: &str) -> Result<Vec<u8>, ApiError> {
    let s = value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| ApiError::bad_request(run, format!("missing string field '{key}'")))?;
    util_b64::decode(s).map_err(|e| ApiError::from_error(run, e))
}

pub async fn healthz(State(st): State<AppState>, headers: HeaderMap) -> Response {
    let run = run_id(&headers);
    ok_json(
        &run,
        json!({
            "service": "lz77b",
            "store_root": st.store.root().display().to_string(),
            "bytes_on_disk": st.store.bytes_on_disk(),
            "store_cap_bytes": st.store.cap_bytes(),
            "stream_output_cap_bytes": st.store.stream_output_cap(),
            "streams": st.store.list_streams().len(),
        }),
    )
}

pub async fn create_stream(
    State(st): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let run = run_id(&headers);
    let value = match json_value(&body, &run) {
        Ok(v) => v,
        Err(e) => return e.into_response(),
    };
    let id = match value.get("stream_id").and_then(Value::as_str) {
        Some(s) => s.to_string(),
        None => {
            // Synthesize an id when the client does not supply one.
            let nanos = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos();
            format!("stream-{nanos:x}")
        }
    };
    if let Err(e) = crate::store::validate_stream_id(&id) {
        return ApiError::from_error(&run, e).into_response();
    }
    let mut sessions = st.sessions.lock().unwrap();
    if sessions.contains_key(&id) {
        return ApiError::from_error(
            &run,
            Error::new(Code::AlreadyExists, format!("stream {id} already exists")),
        )
        .into_response();
    }
    if st.store.block_count(&id).unwrap_or(0) > 0 {
        return ApiError::from_error(
            &run,
            Error::new(
                Code::AlreadyExists,
                format!("stream {id} has blocks on disk"),
            ),
        )
        .into_response();
    }
    sessions.insert(id.clone(), ChainSession::new());
    tracing::info!(run_id = %run, stream_id = %id, "created stream");
    ok_json(&run, json!({ "stream_id": id, "next_index": 0 })).into_response()
}

/// Ensure a session exists, hydrating it from disk blocks if the store has any.
/// Ensure a session exists, hydrating it from disk blocks if the store has any.
///
/// The global sessions lock is held only for the in-memory map lookups and
/// inserts — never across the on-disk replay (which does file I/O plus full
/// CRC/decode per block). Rebuilding a cold session can therefore run
/// concurrently for different streams instead of stalling the whole server.
fn hydrate_session(st: &AppState, id: &str) -> Result<ChainSession, Error> {
    if let Some(s) = st.sessions.lock().unwrap().get(id) {
        return Ok(s.clone());
    }
    // Replay outside the lock. Two concurrent cold requests for the same id
    // may both replay; replay is deterministic, so last-insert is equivalent.
    let session = hydrate_from_disk(st, id)?;
    st.sessions
        .lock()
        .unwrap()
        .entry(id.to_string())
        .or_insert(session.clone());
    Ok(session)
}

pub async fn append_block(
    State(st): State<AppState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let run = run_id(&headers);
    let value = match json_value(&body, &run) {
        Ok(v) => v,
        Err(e) => return e.into_response(),
    };
    let raw = match b64_field(&value, "block", &run) {
        Ok(b) => b,
        Err(e) => return e.into_response(),
    };

    // All chain validation, file I/O and session updates run on the blocking
    // pool so async worker threads are never pinned by disk/CPU work.
    blocking(move || {
        // Validate chain semantics against the live session BEFORE persisting.
        let mut session = match hydrate_session(&st, &id) {
            Ok(s) => s,
            Err(e) => return ApiError::from_error(&run, e).into_response(),
        };
        if let Err(e) = session.decode_raw(&raw) {
            return ApiError::from_error(&run, e).into_response();
        }
        if let Err(e) = st.store.append_block(&id, &raw) {
            // Persistence refused: disk stays authoritative; rebuild the cached
            // session from disk so memory cannot run ahead of durable state.
            if let Ok(fresh) = hydrate_from_disk(&st, &id) {
                st.sessions.lock().unwrap().insert(id.clone(), fresh);
            }
            return ApiError::from_error(&run, e).into_response();
        }
        st.sessions.lock().unwrap().insert(id.clone(), session);
        let h = match BlockHeader::decode(&raw) {
            Ok(h) => h,
            Err(e) => return ApiError::from_error(&run, e).into_response(),
        };
        tracing::info!(run_id = %run, stream_id = %id, index = h.index, "appended block");
        ok_json(
            &run,
            json!({ "stream_id": id, "index": h.index, "bytes": raw.len(), "frame_type": h.frame_type as u8 }),
        )
    })
    .await
}

fn hydrate_from_disk(st: &AppState, id: &str) -> Result<ChainSession, Error> {
    let mut session = ChainSession::new();
    let count = st.store.block_count(id)?;
    for idx in 0..count as u32 {
        let raw = st.store.read_block(id, idx)?;
        session.decode_raw(&raw)?;
    }
    Ok(session)
}

/// Run a request's blocking section (file I/O, codec CPU) on Tokio's blocking
/// pool so async worker threads stay free for connection handling.
async fn blocking<F>(f: F) -> Response
where
    F: FnOnce() -> Response + Send + 'static,
{
    tokio::task::spawn_blocking(f).await.unwrap_or_else(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(
                json!({"ok": false, "error_code": "io", "category": "compute",
                    "detail": "blocking task failed"}),
            ),
        )
            .into_response()
    })
}

pub async fn encode_block_handler(
    State(st): State<AppState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let run = run_id(&headers);
    let value = match json_value(&body, &run) {
        Ok(v) => v,
        Err(e) => return e.into_response(),
    };
    let input = match b64_field(&value, "data", &run) {
        Ok(b) => b,
        Err(e) => return e.into_response(),
    };

    blocking(move || {
        let session = match hydrate_session(&st, &id) {
            Ok(s) => s,
            Err(e) => return ApiError::from_error(&run, e).into_response(),
        };
        let outcome = match encode_next(&session, &input) {
            Ok(o) => o,
            Err(e) => return ApiError::from_error(&run, e).into_response(),
        };
        // Persist before updating the in-memory session. If the subsequent
        // self-decode ever failed, rehydrate from disk so the cache cannot run
        // ahead of durable state.
        if let Err(e) = st.store.append_block(&id, &outcome.raw) {
            return ApiError::from_error(&run, e).into_response();
        }
        let mut advanced = session;
        if let Err(e) = advanced.decode_raw(&outcome.raw) {
            if let Ok(fresh) = hydrate_from_disk(&st, &id) {
                st.sessions.lock().unwrap().insert(id.clone(), fresh);
            }
            return ApiError::from_error(&run, e).into_response();
        }
        st.sessions.lock().unwrap().insert(id.clone(), advanced);
        let s = &outcome.stats;
        tracing::info!(
            run_id = %run, stream_id = %id, index = s.index,
            matches = s.match_tokens, payload_bytes = s.payload_len, "encoded block"
        );
        ok_json(
            &run,
            json!({
                "stream_id": id,
                "index": s.index,
                "frame_type": s.frame_type as u8,
                "block": util_b64::encode(&outcome.raw),
                "input_bytes": s.input_len,
                "payload_bytes": s.payload_len,
                "match_tokens": s.match_tokens,
                "matched_bytes": s.matched_bytes,
                "dict_before": s.dict_before_len,
                "dict_after": s.dict_after_len,
            }),
        )
    })
    .await
}

pub async fn decode_stream(
    State(st): State<AppState>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Response {
    let run = run_id(&headers);
    blocking(move || match st.store.decode_stream(&id) {
        Ok(data) => ok_json(
            &run,
            json!({
                "stream_id": id,
                "bytes": data.len(),
                "data": util_b64::encode(&data),
            }),
        ),
        Err(e) => ApiError::from_error(&run, e).into_response(),
    })
    .await
}

pub async fn encode_independent(headers: HeaderMap, body: Bytes) -> Response {
    let run = run_id(&headers);
    let value = match json_value(&body, &run) {
        Ok(v) => v,
        Err(e) => return e.into_response(),
    };
    let input = match b64_field(&value, "data", &run) {
        Ok(b) => b,
        Err(e) => return e.into_response(),
    };
    match encode_block(FrameType::Independent, 0, &[], &input) {
        Ok(outcome) => {
            let s = &outcome.stats;
            ok_json(
                &run,
                json!({
                    "block": util_b64::encode(&outcome.raw),
                    "input_bytes": s.input_len,
                    "payload_bytes": s.payload_len,
                    "match_tokens": s.match_tokens,
                }),
            )
        }
        Err(e) => ApiError::from_error(&run, e).into_response(),
    }
}

/// Stateless block decode. Body fields:
/// * `block`   — raw block bytes (base64), required
/// * `dict`    — predecessor dictionary bytes (base64), optional/empty for independent
/// * `cross_check` — when true, also run the independent reference decompressor
///   and require identical bytes.
pub async fn decode_one(headers: HeaderMap, body: Bytes) -> Response {
    let run = run_id(&headers);
    let value = match json_value(&body, &run) {
        Ok(v) => v,
        Err(e) => return e.into_response(),
    };
    let raw = match b64_field(&value, "block", &run) {
        Ok(b) => b,
        Err(e) => return e.into_response(),
    };
    let dict = match value.get("dict") {
        Some(Value::String(s)) => match util_b64::decode(s) {
            Ok(b) => b,
            Err(e) => return ApiError::from_error(&run, e).into_response(),
        },
        Some(Value::Null) | None => Vec::new(),
        Some(_) => {
            return ApiError::bad_request(&run, "field 'dict' must be a base64 string or null")
                .into_response()
        }
    };
    let cross_check = value
        .get("cross_check")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    let header = match BlockHeader::decode(&raw) {
        Ok(h) => h,
        Err(e) => return ApiError::from_error(&run, e).into_response(),
    };
    if header.frame_type == FrameType::Independent && !dict.is_empty() {
        return ApiError::from_error(
            &run,
            Error::new(
                Code::DigestMismatch,
                "independent block given a nonempty dict",
            ),
        )
        .into_response();
    }
    let payload = header.payload(&raw);
    if let Err(e) = header.verify_crc(payload) {
        return ApiError::from_error(&run, e).into_response();
    }
    let decoded = match crate::core::decoder::decode_payload(
        header.frame_type,
        payload,
        &dict,
        header.decompressed_len,
    ) {
        Ok(d) => d,
        Err(e) => return ApiError::from_error(&run, e).into_response(),
    };

    let mut cross: Option<Value> = None;
    if cross_check {
        match reference::decompress_raw(&raw, &dict) {
            Ok(ref_bytes) => {
                let agree = ref_bytes == decoded;
                cross = Some(json!({
                    "reference_decompressed": ref_bytes.len(),
                    "reference_agrees": agree,
                }));
                if !agree {
                    return ApiError::from_error(
                        &run,
                        Error::new(
                            Code::CrcMismatch,
                            "core and independent reference decompressor disagree",
                        ),
                    )
                    .into_response();
                }
            }
            Err(re) => {
                return ApiError::from_error(
                    &run,
                    Error::new(
                        match re.kind {
                            reference::RefErrorKind::Input => Code::BadRequest,
                            reference::RefErrorKind::State => Code::DigestMismatch,
                            reference::RefErrorKind::Resource => Code::OutputCapExceeded,
                        },
                        format!("reference decompressor rejected block: {}", re.reason),
                    ),
                )
                .into_response();
            }
        }
    }

    let mut resp = json!({
        "frame_type": header.frame_type as u8,
        "index": header.index,
        "bytes": decoded.len(),
        "data": util_b64::encode(&decoded),
    });
    if let Some(c) = cross {
        resp["cross_check"] = c;
    }
    ok_json(&run, resp)
}
