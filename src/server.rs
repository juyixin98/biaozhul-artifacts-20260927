//! HTTP verification interface (Axum).
//!
//! Endpoints:
//! - `GET  /healthz`                 liveness + loaded set list
//! - `POST /sets`                    build + persist `{name, keys, ...}`
//! - `GET  /sets`                    list sets
//! - `POST /sets/{name}/lookup`      membership probe `{key}`
//! - `GET  /sets/{name}/lookup?key=` same probe via query string
//!
//! Every response carries the `request_id` echoed from the
//! `x-request-id` request header (or generated). Acceptance/rejection always
//! states *why*. Raw key material is never logged; only length and a short
//! non-reversible hash tag are.

use std::sync::Arc;

use axum::{
    body::Bytes,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Extension, Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tracing::{debug, info, warn};

use crate::builder::BuildConfig;
use crate::error::{ErrorKind, MphfError};
use crate::index::{Probe, VerifyMode};
use crate::persistence::Store;

#[derive(Clone)]
pub struct AppState {
    pub store: Store,
    pub defaults: Arc<BuildConfig>,
}

pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/sets", get(list_sets).post(build_set))
        .route("/sets/{name}/lookup", post(lookup).get(lookup_query))
        .layer(middleware::from_fn(request_id_layer))
        .with_state(state)
}

// ---------------------------------------------------------------------------
// Request id middleware + tracing
// ---------------------------------------------------------------------------

async fn request_id_layer(
    headers: HeaderMap,
    mut req: axum::http::Request<axum::body::Body>,
    next: Next,
) -> Response {
    let rid = headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .map(|s| s.to_string())
        .unwrap_or_else(|| {
            // Local, non-cryptographic id is sufficient for log correlation.
            static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
            let seq = COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let tag = std::process::id() as u64
                ^ seq.wrapping_mul(0x9e37_79b9_7f4a_7c15)
                ^ nanos();
            format!("{:016x}", crate::hash::mix64(tag))
        });
    req.extensions_mut().insert(RequestId(rid));
    next.run(req).await
}

#[derive(Clone, Debug)]
struct RequestId(String);

fn nanos() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos() as u64)
        .unwrap_or(0)
}

/// Redacted key descriptor for logs: length plus a short one-way tag.
fn key_tag(key: &[u8]) -> String {
    let h = crate::hash::stream(key, 0x0106_71a6, 0x1a6_1a6_1a6);
    format!("len={},tag={:08x}", key.len(), (h & 0xffff_ffff) as u32)
}

// ---------------------------------------------------------------------------
// DTOs
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct BuildRequest {
    pub name: String,
    /// Keys as UTF-8 strings.
    #[serde(default)]
    pub keys: Vec<String>,
    /// Keys already encoded (base64), merged with `keys`.
    #[serde(default)]
    pub keys_base64: Vec<String>,
    #[serde(default)]
    pub fingerprint_bits: Option<u8>,
    #[serde(default)]
    pub load_factor: Option<f64>,
    #[serde(default)]
    pub max_attempts: Option<u32>,
    #[serde(default)]
    pub base_seed: Option<u64>,
}

#[derive(Debug, Serialize)]
struct BuildResponse {
    request_id: String,
    status: &'static str,
    name: String,
    n: usize,
    duplicates_dropped: usize,
    m: usize,
    seed: u64,
    attempts: u32,
    verify: String,
    elapsed_us: u128,
    attempts_history: serde_json::Value,
}

#[derive(Debug, Deserialize)]
pub struct LookupRequest {
    pub key: String,
    #[serde(default)]
    pub key_base64: Option<String>,
}

#[derive(Debug, Deserialize)]
struct LookupQuery {
    key: String,
}

fn error_body(rid: &str, kind: ErrorKind, message: String) -> serde_json::Value {
    json!({
        "request_id": rid,
        "status": "error",
        "error": kind.as_str(),
        "message": message,
    })
}

struct ApiError {
    status: StatusCode,
    kind: ErrorKind,
    message: String,
}

impl ApiError {
    fn bad_request(msg: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            kind: ErrorKind::BadRequest,
            message: msg.into(),
        }
    }
    fn not_found(msg: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::NOT_FOUND,
            kind: ErrorKind::SetNotFound,
            message: msg.into(),
        }
    }
    fn from_mphf(e: MphfError) -> Self {
        let kind = e.kind();
        let status = match kind {
            ErrorKind::SetNotFound => StatusCode::NOT_FOUND,
            ErrorKind::InvalidInput | ErrorKind::InvalidConfig | ErrorKind::PeelingFailed => {
                StatusCode::BAD_REQUEST
            }
            ErrorKind::FormatMagic
            | ErrorKind::FormatVersion
            | ErrorKind::FormatCorrupt
            | ErrorKind::ChecksumMismatch => StatusCode::INTERNAL_SERVER_ERROR,
            ErrorKind::BadRequest => StatusCode::BAD_REQUEST,
            ErrorKind::Internal => StatusCode::INTERNAL_SERVER_ERROR,
        };
        ApiError {
            status,
            kind,
            message: e.to_string(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        // request_id is attached in handlers via extension where available;
        // for extractor failures we emit without one.
        (
            self.status,
            Json(json!({
                "status": "error",
                "error": self.kind.as_str(),
                "message": self.message,
            })),
        )
            .into_response()
    }
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn healthz(State(s): State<AppState>) -> impl IntoResponse {
    let sets = s.store.list().await.unwrap_or_default();
    Json(json!({
        "status": "ok",
        "algorithm": "bdz3-v1",
        "format_version": crate::format::FORMAT_VERSION,
        "data_dir": s.store.data_dir().display().to_string(),
        "sets": sets,
    }))
}

async fn list_sets(State(s): State<AppState>) -> impl IntoResponse {
    match s.store.list().await {
        Ok(sets) => Json(json!({ "sets": sets })).into_response(),
        Err(e) => ApiError::from_mphf(e).into_response(),
    }
}

fn decode_key(utf8: &str, b64: Option<&str>) -> Result<Vec<u8>, String> {
    if let Some(b) = b64 {
        // Tolerate standard and URL-safe alphabets, plus whitespace.
        let normalized = b.replace('-', "+").replace('_', "/");
        base64_decode(normalized.as_bytes())
    } else {
        Ok(utf8.as_bytes().to_vec())
    }
}

// Minimal base64 decoder without an extra dependency.
fn base64_decode(input: &[u8]) -> Result<Vec<u8>, String> {
    fn val(b: u8) -> Option<u8> {
        match b {
            b'A'..=b'Z' => Some(b - b'A'),
            b'a'..=b'z' => Some(b - b'a' + 26),
            b'0'..=b'9' => Some(b - b'0' + 52),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    }
    let filtered: Vec<u8> = input
        .iter()
        .copied()
        .filter(|b| !b.is_ascii_whitespace())
        .collect();
    if !filtered.len().is_multiple_of(4) {
        return Err("base64 length not a multiple of 4".into());
    }
    let mut out = Vec::with_capacity(filtered.len() / 4 * 3);
    for chunk in filtered.chunks_exact(4) {
        let (mut vals, mut pad) = ([0u8; 4], 0u8);
        for (i, &c) in chunk.iter().enumerate() {
            if c == b'=' {
                vals[i] = 0;
                pad += 1;
            } else {
                if pad > 0 {
                    return Err("data after padding".into());
                }
                vals[i] = val(c).ok_or("bad base64 character")?;
            }
        }
        let (a, b, c, d) = (vals[0], vals[1], vals[2], vals[3]);
        out.push((a << 2) | (b >> 4));
        if pad < 2 {
            out.push((b << 4) | (c >> 2));
        }
        if pad == 0 {
            out.push((c << 6) | d);
        }
        if pad > 2 {
            return Err("invalid padding".into());
        }
    }
    Ok(out)
}

async fn build_set(
    State(s): State<AppState>,
    Extension(rid): Extension<RequestId>,
    raw: Bytes,
) -> Response {
    let req: BuildRequest = match serde_json::from_slice(&raw) {
        Ok(r) => r,
        Err(e) => return ApiError::bad_request(format!("invalid JSON body: {e}")).into_response(),
    };
    if req.keys.is_empty() && req.keys_base64.is_empty() {
        // Empty set is legal; only missing name is not.
    }
    if req.name.is_empty() {
        return ApiError::bad_request("name is required").into_response();
    }

    let mut keys: Vec<Vec<u8>> = Vec::with_capacity(req.keys.len() + req.keys_base64.len());
    for k in &req.keys {
        keys.push(k.as_bytes().to_vec());
    }
    for (i, b) in req.keys_base64.iter().enumerate() {
        match decode_key("", Some(b)) {
            Ok(k) => keys.push(k),
            Err(e) => {
                return ApiError::bad_request(format!("keys_base64[{i}]: {e}")).into_response()
            }
        }
    }

    let mut cfg = (*s.defaults).clone();
    if let Some(bits) = req.fingerprint_bits {
        match VerifyMode::parse(bits) {
            Ok(m) => cfg.verify = m,
            Err(e) => return ApiError::bad_request(e.to_string()).into_response(),
        }
    }
    if let Some(lf) = req.load_factor {
        cfg.load_factor = lf;
    }
    if let Some(a) = req.max_attempts {
        cfg.max_attempts = a;
    }
    if let Some(seed) = req.base_seed {
        cfg.base_seed = seed;
    }
    if let Err(e) = cfg.validate() {
        return ApiError::bad_request(e.to_string()).into_response();
    }

    let input_count = keys.len();
    info!(
        request_id = %rid.0,
        set = %req.name,
        input_keys = input_count,
        verify = ?cfg.verify,
        "build accepted"
    );

    let report = match tokio::task::spawn_blocking(move || crate::builder::build(keys, &cfg))
        .await
    {
        Ok(Ok(r)) => r,
        Ok(Err(e)) => {
            warn!(request_id = %rid.0, error = %e, "build rejected");
            return ApiError::from_mphf(e).into_response();
        }
        Err(e) => {
            return ApiError {
                status: StatusCode::INTERNAL_SERVER_ERROR,
                kind: ErrorKind::Internal,
                message: format!("build task failed: {e}"),
            }
            .into_response();
        }
    };

    let n = report.index.key_count();
    let m = report.index.vertex_count();
    let verify = match report.index.mode {
        VerifyMode::FullKey => "full_key".to_string(),
        VerifyMode::Fingerprint { bits } => format!("fingerprint_{bits}"),
    };
    if let Err(e) = s.store.save(&req.name, report.index).await {
        warn!(request_id = %rid.0, error = %e, "persist failed");
        return ApiError::from_mphf(e).into_response();
    }

    info!(
        request_id = %rid.0,
        set = %req.name,
        n = n,
        duplicates = report.duplicates_dropped,
        seed = report.seed,
        attempts = report.attempts,
        "build succeeded"
    );

    let history = serde_json::to_value(
        report
            .history
            .iter()
            .map(|a| {
                json!({
                    "attempt": a.attempt,
                    "seed": a.seed,
                    "result": a.result,
                    "remaining": a.remaining,
                    "elapsed_us": a.elapsed_us,
                })
            })
            .collect::<Vec<_>>(),
    )
    .unwrap_or(json!([]));

    (
        StatusCode::CREATED,
        Json(BuildResponse {
            request_id: rid.0,
            status: "created",
            name: req.name,
            n,
            duplicates_dropped: report.duplicates_dropped,
            m,
            seed: report.seed,
            attempts: report.attempts,
            verify,
            elapsed_us: report.elapsed_us,
            attempts_history: history,
        }),
    )
        .into_response()
}

async fn lookup(
    State(s): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(name): Path<String>,
    raw: Bytes,
) -> Response {
    let req: LookupRequest = match serde_json::from_slice(&raw) {
        Ok(r) => r,
        Err(e) => return ApiError::bad_request(format!("invalid JSON body: {e}")).into_response(),
    };
    let key = match decode_key(&req.key, req.key_base64.as_deref()) {
        Ok(k) => k,
        Err(e) => return ApiError::bad_request(e).into_response(),
    };
    do_lookup(s, rid, name, key).await
}

async fn lookup_query(
    State(s): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(name): Path<String>,
    Query(q): Query<LookupQuery>,
) -> Response {
    do_lookup(s, rid, name, q.key.into_bytes()).await
}

async fn do_lookup(state: AppState, rid: RequestId, name: String, key: Vec<u8>) -> Response {
    let idx = match state.store.load(&name).await {
        Ok(idx) => idx,
        Err(MphfError::SetNotFound(_)) => {
            return ApiError::not_found(format!("set {name:?} not found")).into_response();
        }
        Err(e) => return ApiError::from_mphf(e).into_response(),
    };
    let tag = key_tag(&key);
    let probe = idx.probe(&key);
    match &probe {
        Probe::Member { slot } => {
            info!(
                request_id = %rid.0,
                set = %name,
                key = %tag,
                slot = slot,
                decision = "accept",
                "member accepted: slot hit with verifier match"
            );
            (
                StatusCode::OK,
                Json(json!({
                    "request_id": rid.0,
                    "set": name,
                    "member": true,
                    "slot": slot,
                    "decision": "accept",
                    "reason": "verifier_match",
                    "key": tag,
                })),
            )
                .into_response()
        }
        Probe::Rejected { reason, slot } => {
            debug!(
                request_id = %rid.0,
                set = %name,
                key = %tag,
                candidate_slot = ?slot,
                decision = "reject",
                reason = reason.as_str(),
                "non-member rejected: verifier disagreement"
            );
            (
                StatusCode::OK,
                Json(json!({
                    "request_id": rid.0,
                    "set": name,
                    "member": false,
                    "candidate_slot": slot,
                    "decision": "reject",
                    "reason": reason.as_str(),
                    "key": tag,
                })),
            )
                .into_response()
        }
        Probe::Inconclusive { reason } => {
            warn!(
                request_id = %rid.0,
                set = %name,
                key = %tag,
                decision = "undetermined",
                reason = reason,
                "undetermined: index does not support a valid probe"
            );
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(error_body(
                    &rid.0,
                    ErrorKind::Internal,
                    format!("undetermined: {reason}"),
                )),
            )
                .into_response()
        }
    }
}
