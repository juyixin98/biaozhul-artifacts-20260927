//! HTTP validation interface (Axum).
//!
//! Every response is an envelope carrying the request id (client-supplied
//! `X-Request-Id` or a generated one), the result, and on failure a typed
//! `error.kind`. Query conventions are documented in [`crate::index`]:
//! half-open `[l, r)` windows, 0-based `k`.

use axum::extract::rejection::JsonRejection;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, RwLock};
use tracing::{info, warn};

use crate::error::WmError;
use crate::format::FORMAT_VERSION;
use crate::index::WmIndex;
use crate::store::IndexStore;

pub const SERVICE_NAME: &str = "wavelet-matrix-service";
pub const SERVICE_VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<IndexStore>,
    pub indexes: Arc<RwLock<HashMap<String, Arc<WmIndex>>>>,
}

impl AppState {
    pub fn new(store: IndexStore) -> Self {
        AppState {
            store: Arc::new(store),
            indexes: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// Load every index found in the data directory. Returns the number
    /// loaded. Corrupt files abort startup so the problem is visible.
    pub fn load_persisted(&self) -> Result<usize, WmError> {
        let names = self.store.list()?;
        for name in &names {
            match self.store.load(name) {
                Ok(index) => {
                    info!(
                        index = %name,
                        len = index.len(),
                        distinct = index.distinct(),
                        height = index.height(),
                        format_version = FORMAT_VERSION,
                        "loaded index from data directory"
                    );
                    self.indexes
                        .write()
                        .expect("index map lock")
                        .insert(name.clone(), Arc::new(index));
                }
                Err(e) => {
                    return Err(WmError::CorruptFormat(format!(
                        "failed to load index {name:?}: {e}"
                    )))
                }
            }
        }
        Ok(names.len())
    }

    pub fn build_router(&self) -> Router {
        Router::new()
            .route("/health", get(health))
            .route("/v1/indexes", get(list_indexes).post(create_index))
            .route("/v1/indexes/{name}", get(get_index))
            .route("/v1/indexes/{name}/queries", post(run_query))
            .with_state(self.clone())
    }
}

#[derive(serde::Serialize)]
struct Envelope {
    request_id: String,
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<ErrorBody>,
}

#[derive(serde::Serialize)]
struct ErrorBody {
    kind: String,
    message: String,
}

fn ok_env(request_id: String, result: Value) -> (StatusCode, Json<Envelope>) {
    (
        StatusCode::OK,
        Json(Envelope {
            request_id,
            ok: true,
            result: Some(result),
            error: None,
        }),
    )
}

fn err_env(request_id: String, status: StatusCode, err: &WmError) -> (StatusCode, Json<Envelope>) {
    (
        status,
        Json(Envelope {
            request_id,
            ok: false,
            result: None,
            error: Some(ErrorBody {
                kind: err.kind().to_string(),
                message: err.to_string(),
            }),
        }),
    )
}

fn status_for(err: &WmError) -> StatusCode {
    match err {
        WmError::IndexNotFound(_) => StatusCode::NOT_FOUND,
        WmError::DuplicateIndex(_) => StatusCode::CONFLICT,
        WmError::EmptyInput
        | WmError::InvalidRange { .. }
        | WmError::EmptyRange { .. }
        | WmError::KOutOfBounds { .. }
        | WmError::InvalidIndexName(_)
        | WmError::BadRequest(_) => StatusCode::BAD_REQUEST,
        WmError::CorruptFormat(_) | WmError::UnsupportedVersion(_) | WmError::Io(_) => {
            StatusCode::INTERNAL_SERVER_ERROR
        }
    }
}

fn fail(request_id: String, err: WmError) -> (StatusCode, Json<Envelope>) {
    let status = status_for(&err);
    warn!(request_id = %request_id, error_kind = err.kind(), %status, "{err}");
    err_env(request_id, status, &err)
}

fn request_id(headers: &HeaderMap) -> String {
    headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.trim())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .map(|s| s.to_string())
        .unwrap_or_else(|| format!("req-{}", uuid::Uuid::new_v4().simple()))
}

fn index_meta(name: &str, idx: &WmIndex) -> Value {
    json!({
        "name": name,
        "len": idx.len(),
        "distinct": idx.distinct(),
        "height": idx.height(),
        "format_version": FORMAT_VERSION,
    })
}

async fn health(State(state): State<AppState>, headers: HeaderMap) -> Json<Envelope> {
    let rid = request_id(&headers);
    let count = state.indexes.read().expect("index map lock").len();
    Json(Envelope {
        request_id: rid,
        ok: true,
        result: Some(json!({
            "status": "ok",
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "indexes_loaded": count,
        })),
        error: None,
    })
}

#[derive(Debug, Deserialize)]
pub struct CreateIndexReq {
    pub name: String,
    pub values: Vec<i64>,
}

async fn create_index(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Result<Json<CreateIndexReq>, JsonRejection>,
) -> (StatusCode, Json<Envelope>) {
    let rid = request_id(&headers);
    let Json(req) = match body {
        Ok(v) => v,
        Err(rejection) => {
            return fail(
                rid,
                WmError::BadRequest(format!("invalid JSON body: {rejection}")),
            )
        }
    };

    info!(request_id = %rid, index = %req.name, n = req.values.len(), "create_index: request received");
    if let Err(e) = IndexStore::validate_name(&req.name) {
        return fail(rid, e);
    }
    if state
        .indexes
        .read()
        .expect("index map lock")
        .contains_key(&req.name)
    {
        return fail(rid, WmError::DuplicateIndex(req.name));
    }

    let index = match WmIndex::build(&req.values) {
        Ok(idx) => idx,
        Err(e) => return fail(rid, e),
    };
    info!(
        request_id = %rid,
        index = %req.name,
        len = index.len(),
        distinct = index.distinct(),
        height = index.height(),
        "create_index: kernel built"
    );

    let path = match state.store.save(&req.name, &index) {
        Ok(p) => p,
        Err(e) => return fail(rid, e),
    };
    let bytes = match std::fs::metadata(&path) {
        Ok(m) => Some(m.len()),
        Err(_) => None,
    };
    info!(
        request_id = %rid,
        index = %req.name,
        path = %path.display(),
        bytes = bytes,
        "create_index: persisted to filesystem"
    );

    state
        .indexes
        .write()
        .expect("index map lock")
        .insert(req.name.clone(), Arc::new(index.clone()));

    let mut result = index_meta(&req.name, &index);
    result["persisted_path"] = json!(path.display().to_string());
    result["persisted_bytes"] = json!(bytes);
    (
        StatusCode::CREATED,
        Json(Envelope {
            request_id: rid,
            ok: true,
            result: Some(result),
            error: None,
        }),
    )
}

async fn list_indexes(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> (StatusCode, Json<Envelope>) {
    let rid = request_id(&headers);
    let map = state.indexes.read().expect("index map lock");
    let mut metas: Vec<Value> = map.iter().map(|(n, i)| index_meta(n, i)).collect();
    metas.sort_by_key(|v| v["name"].as_str().unwrap_or("").to_string());
    ok_env(rid, json!({ "indexes": metas }))
}

async fn get_index(
    State(state): State<AppState>,
    Path(name): Path<String>,
    headers: HeaderMap,
) -> (StatusCode, Json<Envelope>) {
    let rid = request_id(&headers);
    let map = state.indexes.read().expect("index map lock");
    match map.get(&name) {
        Some(idx) => ok_env(rid, index_meta(&name, idx)),
        None => fail(rid, WmError::IndexNotFound(name)),
    }
}

#[derive(Debug, Deserialize)]
pub struct QueryReq {
    pub op: String,
    pub l: usize,
    pub r: usize,
    #[serde(default)]
    pub k: Option<usize>,
    #[serde(default)]
    pub bound: Option<i64>,
    #[serde(default)]
    pub lo: Option<i64>,
    #[serde(default)]
    pub hi: Option<i64>,
}

async fn run_query(
    State(state): State<AppState>,
    Path(name): Path<String>,
    headers: HeaderMap,
    body: Result<Json<QueryReq>, JsonRejection>,
) -> (StatusCode, Json<Envelope>) {
    let rid = request_id(&headers);
    let Json(req) = match body {
        Ok(v) => v,
        Err(rejection) => {
            return fail(
                rid,
                WmError::BadRequest(format!("invalid JSON body: {rejection}")),
            )
        }
    };

    let idx = {
        let map = state.indexes.read().expect("index map lock");
        match map.get(&name) {
            Some(idx) => Arc::clone(idx),
            None => return fail(rid, WmError::IndexNotFound(name)),
        }
    };

    let (l, r) = (req.l, req.r);
    let dispatch = || -> Result<Value, WmError> {
        match req.op.as_str() {
            "kth_smallest" => {
                let k = req
                    .k
                    .ok_or_else(|| WmError::BadRequest("op kth_smallest requires `k`".into()))?;
                let value = idx.kth_smallest(l, r, k)?;
                Ok(json!({ "op": "kth_smallest", "l": l, "r": r, "k": k, "value": value }))
            }
            "count_lt" => {
                let bound = require_bound(req.bound, "count_lt")?;
                let count = idx.count_lt(l, r, bound)?;
                Ok(json!({ "op": "count_lt", "l": l, "r": r, "bound": bound, "count": count }))
            }
            "count_range" => {
                let lo = req
                    .lo
                    .ok_or_else(|| WmError::BadRequest("op count_range requires `lo`".into()))?;
                let hi = req
                    .hi
                    .ok_or_else(|| WmError::BadRequest("op count_range requires `hi`".into()))?;
                let count = idx.count_range(l, r, lo, hi)?;
                Ok(
                    json!({ "op": "count_range", "l": l, "r": r, "lo": lo, "hi": hi, "count": count }),
                )
            }
            "predecessor" => {
                let bound = require_bound(req.bound, "predecessor")?;
                match idx.predecessor(l, r, bound)? {
                    Some(value) => Ok(
                        json!({ "op": "predecessor", "l": l, "r": r, "bound": bound, "found": true, "value": value }),
                    ),
                    None => Ok(
                        json!({ "op": "predecessor", "l": l, "r": r, "bound": bound, "found": false, "value": Value::Null }),
                    ),
                }
            }
            "successor" => {
                let bound = require_bound(req.bound, "successor")?;
                match idx.successor(l, r, bound)? {
                    Some(value) => Ok(
                        json!({ "op": "successor", "l": l, "r": r, "bound": bound, "found": true, "value": value }),
                    ),
                    None => Ok(
                        json!({ "op": "successor", "l": l, "r": r, "bound": bound, "found": false, "value": Value::Null }),
                    ),
                }
            }
            other => Err(WmError::BadRequest(format!(
                "unknown op {other:?}; expected one of \
                 kth_smallest, count_lt, count_range, predecessor, successor"
            ))),
        }
    };

    match dispatch() {
        Ok(result) => {
            info!(
                request_id = %rid,
                index = %name,
                op = %req.op,
                l = l,
                r = r,
                result = %result,
                "query handled by kernel"
            );
            ok_env(rid, result)
        }
        Err(e) => fail(rid, e),
    }
}

fn require_bound(bound: Option<i64>, op: &str) -> Result<i64, WmError> {
    bound.ok_or_else(|| WmError::BadRequest(format!("op {op} requires `bound`")))
}

/// Bind a TCP listener; used by the server binary and kept here so address
/// handling has one home.
pub async fn serve(router: Router, addr: SocketAddr) -> Result<(), WmError> {
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| WmError::Io(format!("bind {addr}: {e}")))?;
    info!(%addr, service = SERVICE_NAME, version = SERVICE_VERSION, "listening");
    axum::serve(listener, router)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .map_err(|e| WmError::Io(format!("server: {e}")))
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
