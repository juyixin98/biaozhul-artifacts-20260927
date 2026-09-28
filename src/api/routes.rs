//! Axum 路由、处理器、请求身份中间件与统一错误映射。

use std::sync::Arc;

use axum::extract::{Path, State};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::Extension;
use axum::{Json, Router};
use serde_json::json;

use crate::error::CoreError;
use crate::model::VersionInfo;
use crate::rect::Rect;
use crate::store::{CommitOutcome, QueryOutcome, RegisterOutcome, Store};
use crate::telemetry::{is_valid_request_id, RunIdentity};

use super::dto::{BatchReq, ErrorBody, ErrorDetail, QueryReq, RegisterReq};
use super::extractor::ApiJson;

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Store>,
    pub run: Arc<RunIdentity>,
    pub max_body_bytes: usize,
}

/// 供提取器取出 run_id 的扩展类型。
#[derive(Clone)]
pub struct RunIdExt(pub String);

const REQ_ID_HEADER: &str = "x-request-id";

pub fn router(state: AppState) -> Router {
    let api = Router::new()
        .route("/v1/tables", post(register_table))
        .route("/v1/tables/:table_id/batches", post(commit_batch))
        .route("/v1/tables/:table_id/query", post(query_rect))
        .route("/v1/tables/:table_id/versions", get(list_versions))
        .route("/v1/tables/:table_id", get(get_table))
        .layer(axum::extract::DefaultBodyLimit::max(state.max_body_bytes));

    Router::new()
        .route("/health", get(health))
        .merge(api)
        .fallback(fallback)
        .layer(middleware::from_fn_with_state(
            state.clone(),
            request_context,
        ))
        .with_state(state)
}

/// 注入/校验请求 id 与 run_id；回写响应头；把非 JSON 的 4xx/5xx 包成错误信封。
async fn request_context(
    headers: HeaderMap,
    State(state): State<AppState>,
    mut req: axum::http::Request<axum::body::Body>,
    next: Next,
) -> Response {
    let request_id = headers
        .get(REQ_ID_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
        .filter(|s| is_valid_request_id(s))
        .unwrap_or_else(|| state.run.next_request_id());
    req.extensions_mut().insert(request_id.clone());
    req.extensions_mut()
        .insert(RunIdExt(state.run.run_id.clone()));

    let mut resp = next.run(req).await;
    if let Ok(v) = HeaderValue::from_str(&request_id) {
        resp.headers_mut().insert(REQ_ID_HEADER, v);
    }
    resp.headers_mut().insert(
        "x-run-id",
        HeaderValue::from_str(&state.run.run_id).unwrap(),
    );

    let status = resp.status();
    if (status.is_client_error() || status.is_server_error())
        && resp
            .headers()
            .get(axum::http::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .map(|ct| !ct.contains("application/json"))
            .unwrap_or(true)
    {
        let code = if status == StatusCode::METHOD_NOT_ALLOWED {
            "METHOD_NOT_ALLOWED"
        } else if status == StatusCode::NOT_FOUND {
            "NOT_FOUND"
        } else {
            "ERROR"
        };
        let body = ErrorBody {
            ok: false,
            error: ErrorDetail {
                code: code.into(),
                message: format!("{status}"),
                request_id,
                run_id: state.run.run_id.clone(),
            },
        };
        let mut json_resp = Json(body).into_response();
        *json_resp.status_mut() = status;
        // 中间件头在 layer 层随后补充，这里保留 status 即可。
        return json_resp;
    }
    resp
}

/// 构造统一错误响应（处理器与提取器共用）。
pub fn error_response(
    status: StatusCode,
    err: CoreError,
    request_id: String,
    run_id: String,
    override_code: Option<&str>,
) -> Response {
    match status {
        StatusCode::INTERNAL_SERVER_ERROR => {
            tracing::error!(code = err.code(), request_id = %request_id, error = %err, "request failed")
        }
        _ => {
            tracing::warn!(code = err.code(), request_id = %request_id, error = %err, "request rejected")
        }
    }
    let body = ErrorBody {
        ok: false,
        error: ErrorDetail {
            code: override_code.unwrap_or_else(|| err.code()).to_string(),
            message: err.to_string(),
            request_id,
            run_id,
        },
    };
    (status, Json(body)).into_response()
}

/// 处理器通用：从扩展取身份，把内核错误映射成 HTTP 错误。
fn fail(err: CoreError, request_id: &str, run_id: &str) -> Response {
    let status =
        StatusCode::from_u16(err.http_status()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    error_response(
        status,
        err,
        request_id.to_string(),
        run_id.to_string(),
        None,
    )
}

// ---------- handlers ----------

async fn health(State(state): State<AppState>) -> impl IntoResponse {
    Json(json!({
        "ok": true,
        "run_id": state.run.run_id,
        "tables": state.store.list_table_ids(),
    }))
}

async fn register_table(
    State(state): State<AppState>,
    Extension(rid): Extension<String>,
    Extension(run_ext): Extension<RunIdExt>,
    ApiJson(input): ApiJson<RegisterReq>,
) -> Response {
    let run = run_ext.0;
    match state.store.register_table(input.xs, input.ys) {
        Ok(o @ RegisterOutcome { .. }) => {
            tracing::info!(request_id = %rid, table_id = o.table_id, nx = o.nx, ny = o.ny, "table registered");
            (StatusCode::CREATED, Json(register_json(o))).into_response()
        }
        Err(e) => fail(e, &rid, &run),
    }
}

async fn commit_batch(
    State(state): State<AppState>,
    Path(table_id): Path<String>,
    Extension(rid): Extension<String>,
    Extension(run_ext): Extension<RunIdExt>,
    ApiJson(input): ApiJson<BatchReq>,
) -> Response {
    let run = run_ext.0;
    let table_id = match parse_table_id(&table_id) {
        Ok(v) => v,
        Err(e) => return fail(e, &rid, &run),
    };
    let base = match parse_optional_version(input.base_version, "base_version") {
        Ok(v) => v,
        Err(e) => return fail(e, &rid, &run),
    };
    match state.store.commit_batch(table_id, base, input.updates) {
        Ok(o @ CommitOutcome { .. }) => {
            tracing::info!(
                request_id = %rid,
                table_id = table_id,
                version = o.version,
                updates = o.update_count,
                "batch committed"
            );
            (StatusCode::OK, Json(commit_json(o))).into_response()
        }
        Err(e) => fail(e, &rid, &run),
    }
}

async fn query_rect(
    State(state): State<AppState>,
    Path(table_id): Path<String>,
    Extension(rid): Extension<String>,
    Extension(run_ext): Extension<RunIdExt>,
    ApiJson(input): ApiJson<QueryReq>,
) -> Response {
    let run = run_ext.0;
    let table_id = match parse_table_id(&table_id) {
        Ok(v) => v,
        Err(e) => return fail(e, &rid, &run),
    };
    let version = match parse_optional_version(input.version, "version") {
        Ok(v) => v,
        Err(e) => return fail(e, &rid, &run),
    };
    let rect: Rect = input.rect;
    match state.store.query(table_id, version, &rect) {
        Ok(o @ QueryOutcome { .. }) => {
            tracing::debug!(
                request_id = %rid,
                table_id = table_id,
                version = o.version,
                sum = o.sum,
                empty = o.empty,
                "rectangle queried"
            );
            (StatusCode::OK, Json(query_json(o, rect))).into_response()
        }
        Err(e) => fail(e, &rid, &run),
    }
}

async fn list_versions(
    State(state): State<AppState>,
    Path(table_id): Path<String>,
    Extension(rid): Extension<String>,
    Extension(run_ext): Extension<RunIdExt>,
) -> Response {
    let run = run_ext.0;
    let table_id = match parse_table_id(&table_id) {
        Ok(v) => v,
        Err(e) => return fail(e, &rid, &run),
    };
    match state.store.with_table(table_id, |t| t.versions()) {
        Ok(versions) => (
            StatusCode::OK,
            Json(json!({
                "ok": true,
                "table_id": table_id,
                "latest_version": versions.len() as u64 - 1,
                "versions": versions.iter().map(version_json).collect::<Vec<_>>(),
            })),
        )
            .into_response(),
        Err(e) => fail(e, &rid, &run),
    }
}

async fn get_table(
    State(state): State<AppState>,
    Path(table_id): Path<String>,
    Extension(rid): Extension<String>,
    Extension(run_ext): Extension<RunIdExt>,
) -> Response {
    let run = run_ext.0;
    let table_id = match parse_table_id(&table_id) {
        Ok(v) => v,
        Err(e) => return fail(e, &rid, &run),
    };
    match state.store.with_table(table_id, |t| {
        json!({
            "ok": true,
            "table_id": table_id,
            "latest_version": t.latest_version(),
            "xs": t.table.xs.values(),
            "ys": t.table.ys.values(),
        })
    }) {
        Ok(v) => (StatusCode::OK, Json(v)).into_response(),
        Err(e) => fail(e, &rid, &run),
    }
}

/// 全局 fallback：已知路径用错方法 → 405；未知路径 → 404。
/// （具体状态由 `request_context` 中间件统一包装成 JSON 信封。）
async fn fallback(method: Method, uri: axum::http::Uri) -> Response {
    let known = uri.path().starts_with("/v1/tables") || uri.path() == "/health";
    if known && method != Method::GET && method != Method::POST {
        StatusCode::METHOD_NOT_ALLOWED.into_response()
    } else {
        StatusCode::NOT_FOUND.into_response()
    }
}

// ---------- 小工具 ----------

fn parse_table_id(raw: &str) -> Result<u32, CoreError> {
    let v: u64 = raw.parse().map_err(|_| {
        CoreError::InvalidRequest(format!("table id must be a positive integer, got {raw:?}"))
    })?;
    if v == 0 || v > u32::MAX as u64 {
        return Err(CoreError::InvalidRequest(format!(
            "table id out of range: {raw}"
        )));
    }
    Ok(v as u32)
}

fn parse_optional_version(raw: Option<i64>, field: &str) -> Result<Option<u64>, CoreError> {
    match raw {
        None => Ok(None),
        Some(v) if v >= 0 => Ok(Some(v as u64)),
        Some(v) => Err(CoreError::InvalidRequest(format!(
            "{field} must be >= 0, got {v}"
        ))),
    }
}

#[allow(dead_code)]
fn parse_optional_u64(raw: Option<i64>, field: &str) -> Result<Option<u64>, CoreError> {
    parse_optional_version(raw, field)
}

fn register_json(o: RegisterOutcome) -> serde_json::Value {
    json!({
        "ok": true,
        "table_id": o.table_id,
        "version": o.version,
        "nx": o.nx,
        "ny": o.ny,
        "duplicate_x": o.duplicate_x,
        "duplicate_y": o.duplicate_y,
        "created_at_ms": o.created_at_ms,
    })
}

fn commit_json(o: CommitOutcome) -> serde_json::Value {
    json!({
        "ok": true,
        "table_id": o.table_id,
        "version": o.version,
        "base_version": o.base_version,
        "update_count": o.update_count,
        "created_at_ms": o.created_at_ms,
    })
}

fn query_json(o: QueryOutcome, rect: Rect) -> serde_json::Value {
    json!({
        "ok": true,
        "table_id": o.table_id,
        "version": o.version,
        "rect": {
            "x_lo": rect.x_lo,
            "x_hi": rect.x_hi,
            "y_lo": rect.y_lo,
            "y_hi": rect.y_hi,
        },
        "sum": o.sum,
        "empty": o.empty,
        "selected_coordinates": { "x": o.selected_x, "y": o.selected_y },
    })
}

fn version_json(v: &VersionInfo) -> serde_json::Value {
    json!({
        "version": v.version,
        "base_version": v.base_version,
        "update_count": v.update_count,
        "created_at_ms": v.created_at_ms,
    })
}
