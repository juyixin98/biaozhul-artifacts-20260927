//! Axum verification interface.
//!
//! Every response is JSON. Successful responses carry `"ok": true`; failures
//! carry `"ok": false`, a stable `error_code`, a message and the `request_id`.
//! A request id is taken from `x-request-id` when supplied (so external test
//! runs can correlate logs) or generated as `rid-<hex>` otherwise.

use crate::errors::{ApiError, ApiResult, AppError};
use crate::model::{PointUpdate, Rect};
use crate::service::Registry;
use axum::body::{to_bytes, Bytes};
use axum::extract::{Extension, Path, State};
use axum::http::{HeaderMap, HeaderName, StatusCode};
use axum::middleware::{self, Next};
use axum::response::Response;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

const RID_HEADER: HeaderName = HeaderName::from_static("x-request-id");

#[derive(Clone)]
pub struct AppState {
    pub registry: Arc<Registry>,
    pub max_body_bytes: usize,
}

/// Build the router. `max_body_bytes` caps buffered request bodies.
pub fn router(registry: Arc<Registry>, max_body_bytes: usize) -> Router {
    let state = AppState {
        registry,
        max_body_bytes,
    };
    Router::new()
        .route("/", get(index))
        .route("/health", get(health))
        .route("/query", get(query_rect))
        .route("/versions", get(list_versions))
        .route("/versions/:version", get(get_version))
        .route("/batches", post(apply_batch))
        .route("/admin/register", post(register_coords))
        .route("/admin/rebuild", post(rebuild_coords))
        .fallback(not_found)
        .layer(middleware::from_fn(request_id_layer))
        .with_state(state)
}

/// Unknown route: JSON 404 carrying the same correlation/error shape.
async fn not_found(
    Extension(rid): Extension<String>,
    uri: axum::http::Uri,
) -> (StatusCode, Json<serde_json::Value>) {
    (
        StatusCode::NOT_FOUND,
        Json(serde_json::json!({
            "ok": false,
            "error_code": "not_found",
            "error": format!("no route for {}", uri.path()),
            "request_id": rid,
        })),
    )
}

async fn index() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "ok": true,
        "service": "prereg2d",
        "boundary_semantics": "inclusive-inclusive rectangles [x_lo,x_hi] x [y_lo,y_hi]; x_lo>x_hi or y_lo>y_hi is an empty rectangle summing to 0",
        "endpoints": [
            "GET /health",
            "POST /admin/register {xs:[i64], ys:[i64]}",
            "POST /admin/rebuild  {xs:[i64], ys:[i64]}",
            "POST /batches {updates:[{x,y,delta}]}",
            "GET  /query?version=&x_lo=&x_hi=&y_lo=&y_hi=",
            "GET  /versions",
            "GET  /versions/:version"
        ]
    }))
}

#[derive(Serialize)]
struct HealthOut {
    ok: bool,
    head_version: Option<u64>,
}

async fn health(State(s): State<AppState>) -> Json<HealthOut> {
    Json(HealthOut {
        ok: true,
        head_version: s.registry.head_version(),
    })
}

// ---------- request bodies ----------

#[derive(Deserialize)]
struct CoordsReq {
    xs: Vec<i64>,
    ys: Vec<i64>,
}

#[derive(Deserialize)]
struct BatchReq {
    updates: Vec<PointUpdate>,
}

// ---------- helpers ----------

fn ok<T: Serialize>(payload: T) -> Json<serde_json::Value> {
    let mut v = serde_json::to_value(payload).expect("handlers serialize");
    if let Some(map) = v.as_object_mut() {
        map.insert("ok".into(), serde_json::json!(true));
    }
    Json(v)
}

fn bad(rid: &str, code: &'static str, message: String) -> ApiError {
    let inner = match code {
        "payload_too_large" => AppError::PayloadTooLarge(message),
        _ => AppError::BadRequest(message),
    };
    inner.with_rid(rid.to_string())
}

/// Buffer and deserialize a JSON body, turning every transport/parse failure
/// into an explicit categorized error (never an opaque 500). An advertised
/// `Content-Length` over the limit fails immediately with `payload_too_large`.
async fn read_json<T: DeserializeOwned>(
    headers: &HeaderMap,
    body: axum::body::Body,
    limit: usize,
    rid: &str,
) -> Result<T, ApiError> {
    if let Some(len) = headers.get(axum::http::header::CONTENT_LENGTH) {
        if let Ok(text) = len.to_str() {
            if let Ok(advertised) = text.parse::<usize>() {
                if advertised > limit {
                    return Err(bad(
                        rid,
                        "payload_too_large",
                        format!("content-length {advertised} exceeds limit {limit}"),
                    ));
                }
            }
        }
    }
    let bytes: Bytes = to_bytes(body, limit).await.map_err(|e| {
        // http_body_util::LengthLimitError surfaces as the chained source.
        let mut is_limit = false;
        let mut src: Option<&(dyn std::error::Error + 'static)> = Some(&e);
        while let Some(err) = src {
            if err.is::<http_body_util::LengthLimitError>() {
                is_limit = true;
                break;
            }
            src = err.source();
        }
        if is_limit {
            bad(
                rid,
                "payload_too_large",
                format!("body exceeds limit {limit}"),
            )
        } else {
            bad(rid, "bad_request", format!("failed reading body: {e}"))
        }
    })?;
    serde_json::from_slice::<T>(&bytes).map_err(|e| {
        let preview: Vec<u8> = bytes.iter().take(128).copied().collect();
        AppError::BadRequest(format!(
            "body is not valid JSON matching schema: {e}; first bytes: {:?}",
            String::from_utf8_lossy(&preview)
        ))
        .with_rid(rid.to_string())
    })
}

// ---------- handlers ----------

async fn register_coords(
    State(s): State<AppState>,
    Extension(rid): Extension<String>,
    headers: HeaderMap,
    body: axum::body::Body,
) -> ApiResult<Json<serde_json::Value>> {
    let body: CoordsReq = read_json(&headers, body, s.max_body_bytes, &rid).await?;
    let info = s
        .registry
        .register(body.xs, body.ys)
        .map_err(|e| e.with_rid(rid))?;
    Ok(ok(info))
}

async fn rebuild_coords(
    State(s): State<AppState>,
    Extension(rid): Extension<String>,
    headers: HeaderMap,
    body: axum::body::Body,
) -> ApiResult<Json<serde_json::Value>> {
    let body: CoordsReq = read_json(&headers, body, s.max_body_bytes, &rid).await?;
    let info = s
        .registry
        .rebuild(body.xs, body.ys)
        .map_err(|e| e.with_rid(rid))?;
    Ok(ok(info))
}

async fn apply_batch(
    State(s): State<AppState>,
    Extension(rid): Extension<String>,
    headers: HeaderMap,
    body: axum::body::Body,
) -> ApiResult<Json<serde_json::Value>> {
    let body: BatchReq = read_json(&headers, body, s.max_body_bytes, &rid).await?;
    let info = s
        .registry
        .apply_batch(body.updates)
        .map_err(|e| e.with_rid(rid))?;
    Ok(ok(info))
}

async fn query_rect(
    State(s): State<AppState>,
    Extension(rid): Extension<String>,
    axum::extract::RawQuery(raw): axum::extract::RawQuery,
) -> ApiResult<Json<serde_json::Value>> {
    // Parse the query string ourselves so a missing/garbled parameter is
    // reported as our categorized `bad_request` JSON rather than axum's
    // plain-text extractor rejection.
    let pairs: std::collections::HashMap<String, String> = match &raw {
        None => std::collections::HashMap::new(),
        Some(q) => serde_urlencoded::from_str(q).map_err(|e| {
            AppError::BadRequest(format!("malformed query string: {e}")).with_rid(rid.clone())
        })?,
    };
    let require = |name: &str| -> Result<String, ApiError> {
        pairs
            .get(name)
            .cloned()
            .filter(|v| !v.is_empty())
            .ok_or_else(|| {
                AppError::BadRequest(format!("missing required query parameter {name}"))
                    .with_rid(rid.clone())
            })
    };
    let parse = |name: &str, raw: &str| -> Result<i128, ApiError> {
        raw.trim().parse::<i128>().map_err(|_| {
            AppError::BadRequest(format!("query parameter {name}={raw:?} is not an integer"))
                .with_rid(rid.clone())
        })
    };
    let version = match pairs.get("version") {
        None => None,
        Some(v) if v.is_empty() || v == "head" => None,
        Some(v) => Some(v.parse::<u64>().map_err(|_| {
            AppError::BadRequest(format!("version={v:?} is not a positive integer or 'head'"))
                .with_rid(rid.clone())
        })?),
    };
    let rect = Rect {
        x_lo: parse("x_lo", &require("x_lo")?)?,
        x_hi: parse("x_hi", &require("x_hi")?)?,
        y_lo: parse("y_lo", &require("y_lo")?)?,
        y_hi: parse("y_hi", &require("y_hi")?)?,
    };
    let outcome = s
        .registry
        .query(version, &rect)
        .map_err(|e| e.with_rid(rid.clone()))?;
    tracing::info!(
        "rectangle query request_id={} version={} sum={} empty={} rect=[{},{}]x[{},{}]",
        rid,
        outcome.version,
        outcome.sum,
        outcome.empty,
        rect.x_lo,
        rect.x_hi,
        rect.y_lo,
        rect.y_hi
    );
    Ok(ok(outcome))
}

async fn list_versions(State(s): State<AppState>) -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "ok": true,
        "head_version": s.registry.head_version(),
        "versions": s.registry.list_versions(),
    }))
}

async fn get_version(
    State(s): State<AppState>,
    Extension(rid): Extension<String>,
    Path(version): Path<String>,
) -> ApiResult<Json<serde_json::Value>> {
    let v = version.parse::<u64>().map_err(|_| {
        AppError::BadRequest(format!("version={version:?} is not a positive integer"))
            .with_rid(rid.clone())
    })?;
    let info = match s
        .registry
        .list_versions()
        .into_iter()
        .find(|i| i.version == v)
    {
        Some(info) => info,
        None => return Err(AppError::UnknownVersion(v).with_rid(rid)),
    };
    Ok(ok(info))
}

// ---------- request id ----------

async fn request_id_layer(
    headers: HeaderMap,
    mut req: axum::http::Request<axum::body::Body>,
    next: Next,
) -> Response {
    let rid = headers
        .get(&RID_HEADER)
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .map(str::to_owned)
        .unwrap_or_else(generate_rid);
    req.extensions_mut().insert(rid);
    next.run(req).await
}

fn generate_rid() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let tag = nanos as u64 ^ std::process::id() as u64;
    format!("rid-{tag:016x}")
}

/// Static status map for tests and documentation cross-checks.
pub fn status_of(code: &str) -> StatusCode {
    match code {
        "unregistered_coordinate"
        | "duplicate_in_batch"
        | "empty_batch"
        | "overflow"
        | "empty_axis"
        | "bad_request" => StatusCode::UNPROCESSABLE_ENTITY,
        "payload_too_large" => StatusCode::PAYLOAD_TOO_LARGE,
        "not_initialized" => StatusCode::PRECONDITION_FAILED,
        "unknown_version" => StatusCode::NOT_FOUND,
        _ => StatusCode::INTERNAL_SERVER_ERROR,
    }
}
