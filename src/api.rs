//! HTTP interface (Axum): encoding, retrieval, integrity inspection and
//! repair endpoints. Every response carries an `x-request-id`; all handler
//! logs are emitted inside a span carrying that id so a request can be traced
//! through encode/audit/reconstruct steps.

use axum::{
    body::Bytes,
    extract::{Path, Query, State},
    http::{header, HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, put, post},
    Extension, Json, Router,
};
use serde::Deserialize;
use serde_json::json;
use tracing::{info, warn};
use uuid::Uuid;

use crate::error::{AppError, AppResult};
use crate::gf256;
use crate::manifest::FORMAT_VERSION;
use crate::service::AppState;
use crate::storage::validate_object_id;

/// Request identity added to every request by middleware.
#[derive(Clone, Debug)]
pub struct RequestId(pub String);

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/config", get(get_config))
        .route("/v1/objects", get(list_objects))
        .route("/v1/objects/{id}", put(put_object).get(get_object))
        .route("/v1/objects/{id}/inspect", get(inspect_object))
        .route("/v1/objects/{id}/repair", post(repair_object))
        .layer(middleware::from_fn(request_id_layer))
        .with_state(state)
}

async fn request_id_layer(
    headers: HeaderMap,
    mut req: axum::http::Request<axum::body::Body>,
    next: Next,
) -> Response {
    // Honor an inbound id for correlation, otherwise generate one.
    let id = headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    req.extensions_mut().insert(RequestId(id.clone()));
    let span = tracing::info_span!("request", request_id = %id);
    let _enter = span.enter();
    let mut resp = next.run(req).await;
    if let Ok(v) = axum::http::HeaderValue::from_str(&id) {
        resp.headers_mut().insert("x-request-id", v);
    }
    resp
}

async fn healthz() -> Json<serde_json::Value> {
    Json(json!({
        "ok": true,
        "service": "ec-service",
        "field": gf256::GF_VERSION,
        "format_version": FORMAT_VERSION,
    }))
}

async fn get_config(State(s): State<AppState>) -> Json<serde_json::Value> {
    Json(json!({
        "allowed_profiles": s.allowed_profiles.iter()
            .map(|(k,m)| json!({"k": k, "m": m}))
            .collect::<Vec<_>>(),
        "max_object_bytes": s.max_object_bytes,
        "field": gf256::GF_VERSION,
        "scheme": "reed-solomon-vandermonde-systematic",
        "format_version": FORMAT_VERSION,
    }))
}

async fn list_objects(State(s): State<AppState>) -> AppResult<Json<serde_json::Value>> {
    let objects = s.store.list_objects().await?;
    Ok(Json(json!({ "objects": objects })))
}

#[derive(Debug, Deserialize)]
struct CodingParams {
    k: Option<u8>,
    m: Option<u8>,
}

async fn put_object(
    State(s): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(id): Path<String>,
    Query(params): Query<CodingParams>,
    body: Bytes,
) -> AppResult<Response> {
    validate_object_id(&id)?;
    let k = params
        .k
        .ok_or_else(|| AppError::bad_request("MISSING_PARAM", "query param k is required"))?;
    let m = params
        .m
        .ok_or_else(|| AppError::bad_request("MISSING_PARAM", "query param m is required"))?;
    info!(
        request_id = %rid.0, object_id = %id, k = k, m = m, bytes = body.len(),
        "PUT object: starting encode pipeline"
    );
    let manifest = s.put_object(&id, &body, k, m).await?;
    let resp = json!({
        "ok": true,
        "object_id": manifest.object_id,
        "k": manifest.k,
        "m": manifest.m,
        "original_len": manifest.original_len,
        "shard_len": manifest.shard_len,
        "pad_len": manifest.pad_len,
        "payload_sha256": manifest.payload_sha256,
        "manifest_digest": manifest.manifest_digest,
        "shards": manifest.shards.iter().map(|r| json!({
            "index": r.index, "role": r.role, "file": r.file,
            "size": r.size, "sha256": r.sha256
        })).collect::<Vec<_>>(),
    });
    Ok((StatusCode::CREATED, Json(resp)).into_response())
}

async fn get_object(
    State(s): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(id): Path<String>,
) -> AppResult<Response> {
    info!(request_id = %rid.0, object_id = %id, "GET object: verify, reconstruct, truncate");
    // inspect first so a failure response still reports *why*.
    let report = s.inspect(&id).await?;
    if !report.recoverable {
        warn!(request_id = %rid.0, object_id = %id, status = %report.status,
              "GET object: below k verified shards, refusing to fabricate");
        return Err(AppError::new(
            StatusCode::CONFLICT,
            "NOT_RECOVERABLE",
            "object has fewer than k verified shards; no data is returned",
        )
        .with_detail(json!({
            "need": report.k,
            "verified": report.ok_shards,
            "missing": report.missing_shards,
            "corrupt": report.corrupt_shards,
            "report": serde_json::to_value(&report).unwrap_or(json!({})),
        })));
    }
    let data = s.get_object(&id).await?;
    let mut resp = (StatusCode::OK, data).into_response();
    let h = resp.headers_mut();
    h.insert(header::CONTENT_TYPE, "application/octet-stream".parse().unwrap());
    h.insert("x-object-id", id.parse().unwrap());
    h.insert("x-original-len", report.original_len.into());
    h.insert("x-payload-sha256", report.payload_sha256.parse().unwrap());
    h.insert("x-object-status", report.status.parse().unwrap());
    Ok(resp)
}

async fn inspect_object(
    State(s): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(id): Path<String>,
) -> AppResult<Json<crate::service::InspectReport>> {
    info!(request_id = %rid.0, object_id = %id, "inspect: classify ok/missing/corrupt shards");
    let report = s.inspect(&id).await?;
    Ok(Json(report))
}

async fn repair_object(
    State(s): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(id): Path<String>,
) -> AppResult<Json<crate::service::RepairReport>> {
    info!(request_id = %rid.0, object_id = %id, "repair: rebuild erasures and re-verify");
    let report = s.repair(&id).await?;
    Ok(Json(report))
}
