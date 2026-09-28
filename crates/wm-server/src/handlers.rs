//! HTTP handlers: index administration and verified queries.

use std::sync::Arc;

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::{Extension, Json};
use serde::Deserialize;
use serde_json::{json, Value};

use wm_format::FORMAT_VERSION;
use wm_store::{IndexMeta, IndexStore};

use crate::api_error::{json_rejection, ApiError};
use crate::request_id::RequestId;
use crate::respond;

/// Shared application state.
#[derive(Clone)]
pub struct AppState {
    pub store: Arc<IndexStore>,
}

fn as_usize(value: u64, field: &str) -> Result<usize, ApiError> {
    usize::try_from(value).map_err(|_| {
        ApiError::new(
            StatusCode::BAD_REQUEST,
            "INTEGER_OUT_OF_RANGE",
            format!(
                "field {field} = {value} exceeds the addressable limit {}",
                usize::MAX
            ),
        )
    })
}

/// Tidy error wrapper that attaches the request id to the envelope.
fn tagged(id: &RequestId, err: ApiError) -> ApiError {
    err.with_request_id(id.as_str())
}

fn context_json(state: &AppState, name: &str, wm: &wm_core::WaveletMatrix) -> Value {
    let location = std::path::Path::new(state.store.path())
        .join(format!("{name}.wmi"))
        .display()
        .to_string();
    respond::index_context(name, &location, FORMAT_VERSION, wm.bit_len(), wm.bit_len())
}

fn meta_json(meta: &IndexMeta) -> Value {
    json!({
        "name": meta.name,
        "len": meta.len,
        "distinct_count": meta.distinct_count,
        "bit_len": meta.bit_len,
        "min": meta.min,
        "max": meta.max,
        "size_bytes": meta.size_bytes,
    })
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CreateIndexRequest {
    /// Non-empty sequence of `i64` values; duplicates and signed extremes
    /// are both legal.
    pub values: Vec<i64>,
    #[serde(default)]
    pub overwrite: bool,
}

/// POST /indexes/:name
pub async fn create_named_index(
    State(state): State<AppState>,
    Extension(id): Extension<RequestId>,
    Path(name): Path<String>,
    body: Result<Json<CreateIndexRequest>, axum::extract::rejection::JsonRejection>,
) -> Result<impl IntoResponse, ApiError> {
    let Json(req) = body.map_err(|e| tagged(&id, json_rejection(e.body_text())))?;
    let wm = state
        .store
        .create_index(&name, &req.values, req.overwrite)
        .map_err(|e| tagged(&id, e.into()))?;

    let file = std::path::Path::new(state.store.path()).join(format!("{name}.wmi"));
    let size_bytes = std::fs::metadata(&file).map(|m| m.len()).unwrap_or(0);

    tracing::info!(
        request_id = id.as_str(),
        index = %name,
        len = wm.len(),
        distinct = wm.distinct_count(),
        bit_len = wm.bit_len(),
        overwrite = req.overwrite,
        "index built and persisted"
    );

    let data = json!({
        "name": name,
        "len": wm.len(),
        "distinct_count": wm.distinct_count(),
        "bit_len": wm.bit_len(),
        "min": wm.distinct_values().first(),
        "max": wm.distinct_values().last(),
        "size_bytes": size_bytes,
    });
    Ok((
        StatusCode::CREATED,
        Json(respond::ok(
            id.as_str(),
            data,
            Some(json!({
                "index": context_json(&state, &name, &wm),
                "steps": summary_steps(wm.len(), wm.distinct_count(), wm.bit_len()),
            })),
        )),
    ))
}

fn summary_steps(n: usize, distinct: usize, bit_len: usize) -> Value {
    json!([
        {"step": 1, "action": "collect_distinct", "distinct": distinct},
        {"step": 2, "action": "order_preserving_compression", "ids": n, "max_id": distinct.saturating_sub(1)},
        {"step": 3, "action": "build_wavelet_levels", "levels": bit_len},
        {"step": 4, "action": "encode_versioned_container", "format_version": FORMAT_VERSION},
        {"step": 5, "action": "atomic_rename_into_place"},
    ])
}

/// GET /indexes
pub async fn list_indexes(
    State(state): State<AppState>,
    Extension(id): Extension<RequestId>,
) -> Result<impl IntoResponse, ApiError> {
    let listing = state.store.list().map_err(|e| tagged(&id, e.into()))?;
    tracing::info!(
        request_id = id.as_str(),
        count = listing.indexes.len(),
        corrupt = listing.corrupt.len(),
        "listed indexes"
    );
    let data = json!({
        "data_dir": state.store.path().display().to_string(),
        "indexes": listing.indexes.values().map(meta_json).collect::<Vec<_>>(),
        "corrupt_entries": listing.corrupt,
    });
    Ok(Json(respond::ok(id.as_str(), data, None)))
}

/// GET /indexes/:name
pub async fn get_index(
    State(state): State<AppState>,
    Extension(id): Extension<RequestId>,
    Path(name): Path<String>,
) -> Result<impl IntoResponse, ApiError> {
    let wm = state.store.load(&name).map_err(|e| tagged(&id, e.into()))?;
    let file = std::path::Path::new(state.store.path()).join(format!("{name}.wmi"));
    let size_bytes = std::fs::metadata(&file).map(|m| m.len()).unwrap_or(0);
    let meta = IndexMeta {
        name: name.clone(),
        len: wm.len(),
        distinct_count: wm.distinct_count(),
        bit_len: wm.bit_len(),
        min: wm.distinct_values().first().copied().unwrap_or(0),
        max: wm.distinct_values().last().copied().unwrap_or(0),
        size_bytes,
    };
    Ok(Json(respond::ok(id.as_str(), meta_json(&meta), None)))
}

/// DELETE /indexes/:name
pub async fn delete_index(
    State(state): State<AppState>,
    Extension(id): Extension<RequestId>,
    Path(name): Path<String>,
) -> Result<impl IntoResponse, ApiError> {
    state
        .store
        .delete(&name)
        .map_err(|e| tagged(&id, e.into()))?;
    tracing::info!(request_id = id.as_str(), index = %name, "index deleted");
    Ok(Json(respond::ok(
        id.as_str(),
        json!({"deleted": name}),
        None,
    )))
}

/// Query request body, tagged by the required `op`.
#[derive(Debug, Deserialize)]
#[serde(tag = "op", deny_unknown_fields)]
pub enum QueryRequest {
    /// Range k-th smallest; `k` is 0-based.
    #[serde(rename = "quantile")]
    Quantile {
        index: String,
        l: u64,
        r: u64,
        k: u64,
    },
    /// Count values in the half-open value range `[lo, hi)`.
    #[serde(rename = "count")]
    Count {
        index: String,
        l: u64,
        r: u64,
        lo: i64,
        hi: i64,
    },
    /// Largest value strictly below `x`.
    #[serde(rename = "predecessor")]
    Predecessor {
        index: String,
        l: u64,
        r: u64,
        x: i64,
    },
    /// Smallest value strictly above `x`.
    #[serde(rename = "successor")]
    Successor {
        index: String,
        l: u64,
        r: u64,
        x: i64,
    },
}

/// POST /query
pub async fn query(
    State(state): State<AppState>,
    Extension(id): Extension<RequestId>,
    body: Result<Json<QueryRequest>, axum::extract::rejection::JsonRejection>,
) -> Result<impl IntoResponse, ApiError> {
    let Json(req) = body.map_err(|e| tagged(&id, json_rejection(e.body_text())))?;

    let (index, l, r) = match &req {
        QueryRequest::Quantile { index, l, r, .. }
        | QueryRequest::Count { index, l, r, .. }
        | QueryRequest::Predecessor { index, l, r, .. }
        | QueryRequest::Successor { index, l, r, .. } => (index.clone(), *l, *r),
    };
    let l = as_usize(l, "l")?;
    let r = as_usize(r, "r")?;

    // Indexes are loaded from the persisted image for every query, proving
    // the build -> save -> load round trip is what answers requests.
    let wm = state
        .store
        .load(&index)
        .map_err(|e| tagged(&id, e.into()))?;

    let (data, trace, op) = match req {
        QueryRequest::Quantile { k, .. } => {
            let trace = wm
                .explain_quantile(l, r, k)
                .map_err(|e| tagged(&id, e.into()))?;
            tracing::info!(
                request_id = id.as_str(),
                index = %index,
                op = "quantile",
                l = l,
                r = r,
                k = k,
                value = trace.value,
                levels_traversed = trace.steps.len(),
                "query ok"
            );
            (
                json!({"op": "quantile", "l": l, "r": r, "k": k, "value": trace.value}),
                respond::quantile_trace_json(&trace),
                "quantile",
            )
        }
        QueryRequest::Count { lo, hi, .. } => {
            let trace = wm
                .explain_range_count(l, r, lo, hi)
                .map_err(|e| tagged(&id, e.into()))?;
            tracing::info!(
                request_id = id.as_str(),
                index = %index,
                op = "count",
                l = l,
                r = r,
                lo = lo,
                hi = hi,
                count = trace.count,
                "query ok"
            );
            (
                json!({"op": "count", "l": l, "r": r, "lo": lo, "hi": hi, "count": trace.count}),
                respond::count_trace_json(&trace),
                "count",
            )
        }
        QueryRequest::Predecessor { x, .. } => {
            let trace = wm
                .explain_predecessor(l, r, x)
                .map_err(|e| tagged(&id, e.into()))?;
            tracing::info!(
                request_id = id.as_str(),
                index = %index,
                op = "predecessor",
                x = x,
                value = ?trace.value,
                present = trace.present,
                "query ok"
            );
            (
                json!({"op": "predecessor", "l": l, "r": r, "x": x,
                       "value": trace.value, "present": trace.present,
                       "exists": trace.value.is_some()}),
                respond::neighbor_trace_json(&trace),
                "predecessor",
            )
        }
        QueryRequest::Successor { x, .. } => {
            let trace = wm
                .explain_successor(l, r, x)
                .map_err(|e| tagged(&id, e.into()))?;
            tracing::info!(
                request_id = id.as_str(),
                index = %index,
                op = "successor",
                x = x,
                value = ?trace.value,
                present = trace.present,
                "query ok"
            );
            (
                json!({"op": "successor", "l": l, "r": r, "x": x,
                       "value": trace.value, "present": trace.present,
                       "exists": trace.value.is_some()}),
                respond::neighbor_trace_json(&trace),
                "successor",
            )
        }
    };

    let diagnostics = json!({
        "index": context_json(&state, &index, &wm),
        "op": op,
        "trace": trace,
    });
    Ok(Json(respond::ok(id.as_str(), data, Some(diagnostics))))
}
