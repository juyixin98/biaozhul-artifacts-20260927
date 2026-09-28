//! HTTP validation interface (axum). Handlers stay thin: all hashing
//! and verification logic lives in `kernel`, persistence in `store`.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, RwLock};

use axum::extract::{Extension, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};

use crate::config::ServiceConfig;
use crate::diag::{DecisionRecord, RequestId};
use crate::hash::masked_key_id;
use crate::kernel::{self, Lookup, MphIndex, NotMemberReason};
use crate::store;

pub struct AppState {
    pub index: RwLock<Option<MphIndex>>,
    pub cfg: ServiceConfig,
    pub req_counter: AtomicU64,
}

pub fn router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/v1/health", get(health))
        .route("/v1/stats", get(stats))
        .route("/v1/query", get(query_get).post(query_post))
        .route("/v1/index/build", post(build_index))
        .route("/v1/index/load", post(load_index))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            request_id_middleware,
        ))
        .with_state(state)
}

async fn request_id_middleware(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    mut req: axum::extract::Request,
    next: Next,
) -> Response {
    let id = headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .map(|s| RequestId(s.to_string()))
        .unwrap_or_else(|| RequestId::new(state.req_counter.fetch_add(1, Ordering::Relaxed)));
    req.extensions_mut().insert(id.clone());
    let mut resp = next.run(req).await;
    resp.headers_mut()
        .insert("x-request-id", id.0.parse().unwrap());
    resp
}

fn rid(req: &axum::extract::Request) -> RequestId {
    req.extensions()
        .get::<RequestId>()
        .cloned()
        .unwrap_or_else(|| RequestId("req-unknown".into()))
}

// ---------- health / stats ----------

async fn health(State(st): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let loaded = st.index.read().unwrap().is_some();
    Json(serde_json::json!({
        "status": "ok",
        "index_loaded": loaded,
    }))
}

async fn stats(State(st): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let guard = st.index.read().unwrap();
    match guard.as_ref() {
        Some(idx) => Json(serde_json::json!({
            "index_loaded": true,
            "n": idx.n,
            "m": idx.m,
            "seed": idx.seed,
            "format_version": crate::format::FORMAT_VERSION,
            "algorithm_version": crate::format::ALGORITHM_VERSION,
        })),
        None => Json(serde_json::json!({ "index_loaded": false })),
    }
}

// ---------- query ----------

#[derive(Deserialize)]
pub struct QueryParams {
    key: String,
}

#[derive(Deserialize)]
pub struct QueryBody {
    key: String,
}

async fn query_get(
    state: State<Arc<AppState>>,
    rid: Extension<RequestId>,
    Query(params): Query<QueryParams>,
) -> Response {
    decide(state, rid, params.key)
}

async fn query_post(
    state: State<Arc<AppState>>,
    rid: Extension<RequestId>,
    Json(body): Json<QueryBody>,
) -> Response {
    decide(state, rid, body.key)
}

fn decide(
    State(st): State<Arc<AppState>>,
    Extension(rid): Extension<RequestId>,
    raw_key: String,
) -> Response {
    let key = raw_key.as_bytes();
    let key_id = masked_key_id(key);

    let guard = st.index.read().unwrap();
    let record = match guard.as_ref() {
        None => DecisionRecord {
            request_id: rid.0.clone(),
            key_id: key_id.clone(),
            decision: "undecidable",
            slot: None,
            reason: "no index loaded; cannot determine membership".to_string(),
            index_seed: None,
            index_n: None,
        },
        Some(idx) => match idx.lookup(key) {
            Lookup::Member { slot } => DecisionRecord {
                request_id: rid.0.clone(),
                key_id: key_id.clone(),
                decision: "member",
                slot: Some(slot),
                reason: "fingerprint verified at candidate slot".to_string(),
                index_seed: Some(idx.seed),
                index_n: Some(idx.n),
            },
            Lookup::NotMember { reason } => {
                let text = match reason {
                    NotMemberReason::EmptyIndex => {
                        "index is empty; no key can be a member".to_string()
                    }
                    NotMemberReason::FingerprintMismatch { candidate_slot } => format!(
                        "fingerprint mismatch at candidate slot {candidate_slot}; \
                         key is not in the indexed set"
                    ),
                };
                DecisionRecord {
                    request_id: rid.0.clone(),
                    key_id: key_id.clone(),
                    decision: "not_member",
                    slot: None,
                    reason: text,
                    index_seed: Some(idx.seed),
                    index_n: Some(idx.n),
                }
            }
        },
    };
    tracing::info!(
        request_id = %record.request_id,
        key_id = %record.key_id,
        decision = record.decision,
        slot = ?record.slot,
        reason = %record.reason,
        "query decision"
    );
    (StatusCode::OK, Json(record)).into_response()
}

// ---------- build ----------

#[derive(Deserialize)]
pub struct BuildRequest {
    keys: Vec<String>,
    seed: Option<u64>,
    max_attempts: Option<u32>,
    /// Persist to `index_path` (default true).
    persist: Option<bool>,
}

#[derive(Serialize)]
pub struct BuildResponse {
    request_id: String,
    status: &'static str,
    n: usize,
    m: usize,
    seed: u64,
    attempts: u32,
    duplicates_removed: usize,
    persisted_to: Option<String>,
}

#[derive(Serialize)]
pub struct ErrorBody {
    request_id: String,
    category: &'static str,
    message: String,
}

fn build_error(rid: &RequestId, e: &kernel::BuildError) -> Response {
    let category = match e {
        kernel::BuildError::PeelingExhausted { .. } => "peeling_exhausted",
        kernel::BuildError::TooManyKeys { .. } => "too_many_keys",
    };
    tracing::warn!(request_id = %rid.0, category, error = %e, "build failed");
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        Json(ErrorBody {
            request_id: rid.0.clone(),
            category,
            message: e.to_string(),
        }),
    )
        .into_response()
}

async fn build_index(
    State(st): State<Arc<AppState>>,
    req: axum::extract::Request,
) -> Response {
    let rid = rid(&req);
    let body = axum::body::to_bytes(req.into_body(), 16 * 1024 * 1024).await;
    let Ok(body) = body else {
        return (
            StatusCode::PAYLOAD_TOO_LARGE,
            Json(ErrorBody {
                request_id: rid.0.clone(),
                category: "body_too_large",
                message: "request body exceeds limit".into(),
            }),
        )
            .into_response();
    };
    let parsed: Result<BuildRequest, _> = serde_json::from_slice(&body);
    let req_body = match parsed {
        Ok(r) => r,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(ErrorBody {
                    request_id: rid.0.clone(),
                    category: "bad_request",
                    message: format!("invalid JSON body: {e}"),
                }),
            )
                .into_response()
        }
    };

    let seed = req_body.seed.unwrap_or(st.cfg.default_seed);
    let max_attempts = req_body
        .max_attempts
        .unwrap_or(st.cfg.default_max_attempts);
    let keys: Vec<Vec<u8>> = req_body.keys.iter().map(|k| k.clone().into_bytes()).collect();

    let outcome = match kernel::build(&keys, seed, max_attempts, st.cfg.max_keys) {
        Ok(o) => o,
        Err(e) => return build_error(&rid, &e),
    };

    let persist = req_body.persist.unwrap_or(true);
    let mut persisted_to = None;
    if persist {
        let path = std::path::PathBuf::from(&st.cfg.index_path);
        if let Err(e) = store::save(&outcome.index, &path) {
            tracing::error!(request_id = %rid.0, error = %e, "persist failed");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(ErrorBody {
                    request_id: rid.0.clone(),
                    category: "persist_failed",
                    message: e.to_string(),
                }),
            )
                .into_response();
        }
        persisted_to = Some(st.cfg.index_path.clone());
    }

    let resp = BuildResponse {
        request_id: rid.0.clone(),
        status: "ok",
        n: outcome.index.n,
        m: outcome.index.m,
        seed: outcome.seed,
        attempts: outcome.attempts,
        duplicates_removed: outcome.duplicates_removed,
        persisted_to,
    };
    tracing::info!(
        request_id = %rid.0,
        n = resp.n,
        m = resp.m,
        seed = resp.seed,
        attempts = resp.attempts,
        duplicates_removed = resp.duplicates_removed,
        "build completed"
    );
    *st.index.write().unwrap() = Some(outcome.index);
    (StatusCode::OK, Json(resp)).into_response()
}

// ---------- load ----------

#[derive(Deserialize)]
pub struct LoadRequest {
    path: Option<String>,
}

async fn load_index(
    State(st): State<Arc<AppState>>,
    req: axum::extract::Request,
) -> Response {
    let rid = rid(&req);
    let body = axum::body::to_bytes(req.into_body(), 1024 * 1024)
        .await
        .unwrap_or_default();
    let path = if body.is_empty() {
        st.cfg.index_path.clone()
    } else {
        match serde_json::from_slice::<LoadRequest>(&body) {
            Ok(parsed) => parsed.path.unwrap_or_else(|| st.cfg.index_path.clone()),
            Err(e) => {
                return (
                    StatusCode::BAD_REQUEST,
                    Json(ErrorBody {
                        request_id: rid.0.clone(),
                        category: "bad_request",
                        message: format!("invalid JSON body: {e}"),
                    }),
                )
                    .into_response();
            }
        }
    };
    match store::load(std::path::Path::new(&path)) {
        Ok(idx) => {
            let (n, m, seed) = (idx.n, idx.m, idx.seed);
            *st.index.write().unwrap() = Some(idx);
            tracing::info!(request_id = %rid.0, path = %path, n, m, seed, "index loaded");
            (
                StatusCode::OK,
                Json(serde_json::json!({
                    "request_id": rid.0,
                    "status": "ok",
                    "path": path,
                    "n": n,
                    "m": m,
                    "seed": seed,
                })),
            )
                .into_response()
        }
        Err(e) => {
            tracing::warn!(request_id = %rid.0, path = %path, error = %e, "load failed");
            (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(ErrorBody {
                    request_id: rid.0.clone(),
                    category: "load_failed",
                    message: e.to_string(),
                }),
            )
                .into_response()
        }
    }
}
