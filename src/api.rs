//! Axum HTTP 适配层：只做协议与序列化，业务判定全部来自 [`crate::service::Service`]。
//!
//! 错误不与成功混用：每种失败映射到确定的状态码与 `error.code`（见 [`ErrorCode`]），
//! 未知/损坏状态返回 5xx 而不是 200。每个请求带 `x-request-id`（客户端可传入，缺省
//! 服务端生成），响应头回显，且与日志字段关联。

use std::sync::Arc;

use axum::{
    body::Bytes,
    extract::{Request, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Extension, Json, Router,
};
use serde::{Deserialize, Serialize};

use crate::credentials::CredentialError;
use crate::service::{Service, ServiceError};

/// 应用共享状态。
#[derive(Clone)]
pub struct AppState {
    pub service: Arc<Service>,
    pub run_id: String,
}

/// 结构化错误码（字符串稳定，便于测试断言与客户端处理）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    InvalidRequest,
    KeyEmpty,
    KeyTooLarge,
    FilterFull,
    DuplicateLimit,
    TokenMalformed,
    TokenBadSignature,
    TokenUnsupportedVersion,
    TokenUnknownKey,
    TokenOrdinalNeverIssued,
    TokenReplayed,
    InternalInvariant,
    PersistenceFailed,
    NotFound,
    MethodNotAllowed,
}

impl ErrorCode {
    fn status(self) -> StatusCode {
        match self {
            ErrorCode::InvalidRequest
            | ErrorCode::KeyEmpty
            | ErrorCode::KeyTooLarge
            | ErrorCode::TokenMalformed
            | ErrorCode::TokenUnsupportedVersion => StatusCode::BAD_REQUEST,
            ErrorCode::FilterFull | ErrorCode::DuplicateLimit => StatusCode::SERVICE_UNAVAILABLE,
            ErrorCode::TokenBadSignature
            | ErrorCode::TokenUnknownKey
            | ErrorCode::TokenOrdinalNeverIssued
            | ErrorCode::TokenReplayed => StatusCode::FORBIDDEN,
            ErrorCode::InternalInvariant | ErrorCode::PersistenceFailed => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
            ErrorCode::NotFound | ErrorCode::MethodNotAllowed => StatusCode::NOT_FOUND,
        }
    }
}

#[derive(Debug, Serialize)]
struct ErrorBody {
    ok: bool,
    error: ErrorEnvelope,
}

#[derive(Debug, Serialize)]
struct ErrorEnvelope {
    code: ErrorCode,
    message: String,
    request_id: String,
}

fn map_service_error(e: &ServiceError) -> (StatusCode, ErrorCode) {
    match e {
        ServiceError::FilterFull => (StatusCode::SERVICE_UNAVAILABLE, ErrorCode::FilterFull),
        ServiceError::DuplicateLimit => {
            (StatusCode::SERVICE_UNAVAILABLE, ErrorCode::DuplicateLimit)
        }
        ServiceError::Credential(c) => (
            StatusCode::FORBIDDEN,
            match c {
                CredentialError::MalformedToken => ErrorCode::TokenMalformed,
                CredentialError::BadSignature => ErrorCode::TokenBadSignature,
                CredentialError::UnsupportedTokenVersion => ErrorCode::TokenUnsupportedVersion,
                CredentialError::UnknownKey => ErrorCode::TokenUnknownKey,
                CredentialError::OrdinalNeverIssued => ErrorCode::TokenOrdinalNeverIssued,
                CredentialError::TokenReplayed => ErrorCode::TokenReplayed,
            },
        ),
        ServiceError::Invariant(_) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            ErrorCode::InternalInvariant,
        ),
        ServiceError::Persist(_) | ServiceError::SnapshotCorrupt(_) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            ErrorCode::PersistenceFailed,
        ),
        ServiceError::BadKey(s) => {
            if s.contains("为空") {
                (StatusCode::BAD_REQUEST, ErrorCode::KeyEmpty)
            } else {
                (StatusCode::BAD_REQUEST, ErrorCode::KeyTooLarge)
            }
        }
    }
}

/// 请求上下文（中间件注入）。
#[derive(Clone, Debug)]
struct Ctx {
    request_id: String,
}

/// 构造路由。
pub fn router(service: Arc<Service>, run_id: String) -> Router {
    let state = AppState { service, run_id };
    let mw = axum::middleware::from_fn_with_state(state.clone(), ctx_middleware);
    Router::new()
        .route("/healthz", get(healthz))
        .route("/stats", get(stats))
        .route("/filter/insert", post(insert))
        .route("/filter/contains", post(contains))
        .route("/filter/delete", post(delete))
        .fallback(not_found)
        .layer(mw)
        .with_state(state)
}

async fn ctx_middleware(
    State(state): State<AppState>,
    headers: HeaderMap,
    mut req: Request,
    next: axum::middleware::Next,
) -> Response {
    let rid = headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .unwrap_or_else(new_request_id);
    tracing::debug!(
        request_id = %rid, run_id = %state.run_id,
        "收到请求 {} {}", req.method(), req.uri().path()
    );
    req.extensions_mut().insert(Ctx {
        request_id: rid.clone(),
    });
    let mut resp = next.run(req).await;
    if let Ok(v) = HeaderValue::from_str(&rid) {
        resp.headers_mut().insert("x-request-id", v);
    }
    if let Ok(v) = HeaderValue::from_str(&state.run_id) {
        resp.headers_mut().insert("x-run-id", v);
    }
    resp
}

fn new_request_id() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut buf = [0u8; 8];
    let _ = getrandom::getrandom(&mut buf);
    format!("r-{nanos:x}-{}", hex_short(&buf))
}

fn hex_short(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect::<String>()
}

fn err_response(ctx: &Ctx, code: ErrorCode, message: String) -> Response {
    let status = code.status();
    let body = ErrorBody {
        ok: false,
        error: ErrorEnvelope {
            code,
            message,
            request_id: ctx.request_id.clone(),
        },
    };
    let mut resp = (status, Json(body)).into_response();
    insert_ctx_headers(&mut resp, ctx);
    resp
}

fn insert_ctx_headers(resp: &mut Response, ctx: &Ctx) {
    if let Ok(v) = ctx.request_id.parse() {
        resp.headers_mut().insert("x-request-id", v);
    }
}

async fn healthz(State(s): State<AppState>, Extension(ctx): Extension<Ctx>) -> Response {
    let stats = s.service.stats();
    let body = serde_json::json!({
        "ok": true,
        "status": "serving",
        "version": stats.version,
        "run_id": stats.run_id,
        "kernel_version": stats.kernel_version,
        "snapshot_format_version": stats.snapshot_format_version,
        "token_version": stats.token_version,
    });
    let mut resp = Json(body).into_response();
    insert_ctx_headers(&mut resp, &ctx);
    resp
}

#[derive(Serialize)]
struct StatsResponse {
    ok: bool,
    stats: crate::service::Stats,
}

async fn stats(State(s): State<AppState>, Extension(ctx): Extension<Ctx>) -> Response {
    let st = s.service.stats();
    tracing::info!(request_id = %ctx.request_id, run_id = %s.run_id,
        occupied = st.occupied_slots, load = %st.load_factor, "查询统计");
    let mut resp = Json(StatsResponse {
        ok: true,
        stats: st,
    })
    .into_response();
    insert_ctx_headers(&mut resp, &ctx);
    resp
}

#[derive(Deserialize)]
struct KeyBody {
    key: String,
    /// 可选：仅测试用，固定迁移 RNG 种子，使迁移环可复现。
    #[serde(default)]
    rng_seed: Option<u64>,
}

#[derive(Serialize)]
struct InsertResponse {
    ok: bool,
    newly_occupied: bool,
    duplicate: bool,
    live_count: u64,
    kicks: u32,
    delete_token: String,
}

async fn insert(
    State(s): State<AppState>,
    Extension(ctx): Extension<Ctx>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let parsed: KeyBody = match parse_json(&body, headers.get(header::CONTENT_TYPE)) {
        Ok(v) => v,
        Err(code) => {
            return err_response(&ctx, code, "请求体必须是包含字符串字段 key 的 JSON".into())
        }
    };
    if parsed.key.is_empty() {
        return err_response(&ctx, ErrorCode::KeyEmpty, "key 不能为空".into());
    }
    match s.service.insert(parsed.key.as_bytes(), parsed.rng_seed) {
        Ok(rcpt) => {
            tracing::info!(request_id = %ctx.request_id, run_id = %s.run_id,
                newly_occupied = rcpt.newly_occupied, kicks = rcpt.kicks,
                live = rcpt.live_count, "插入成功");
            let mut resp = Json(InsertResponse {
                ok: true,
                newly_occupied: rcpt.newly_occupied,
                duplicate: !rcpt.newly_occupied,
                live_count: rcpt.live_count,
                kicks: rcpt.kicks,
                delete_token: rcpt.token,
            })
            .into_response();
            insert_ctx_headers(&mut resp, &ctx);
            resp
        }
        Err(e) => {
            let (_, code) = map_service_error(&e);
            tracing::warn!(request_id = %ctx.request_id, run_id = %s.run_id,
                code = ?code, error = %e, "插入失败");
            err_response(&ctx, code, e.to_string())
        }
    }
}

#[derive(Serialize)]
struct ContainsResponse {
    ok: bool,
    member: bool,
}

async fn contains(
    State(s): State<AppState>,
    Extension(ctx): Extension<Ctx>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let parsed: KeyBody = match parse_json(&body, headers.get(header::CONTENT_TYPE)) {
        Ok(v) => v,
        Err(code) => {
            return err_response(&ctx, code, "请求体必须是包含字符串字段 key 的 JSON".into())
        }
    };
    if parsed.key.is_empty() {
        return err_response(&ctx, ErrorCode::KeyEmpty, "key 不能为空".into());
    }
    let m = s.service.contains(parsed.key.as_bytes());
    tracing::debug!(request_id = %ctx.request_id, run_id = %s.run_id, member = m, "成员查询");
    let mut resp = Json(ContainsResponse {
        ok: true,
        member: m,
    })
    .into_response();
    insert_ctx_headers(&mut resp, &ctx);
    resp
}

#[derive(Deserialize)]
struct DeleteBody {
    delete_token: String,
}

#[derive(Serialize)]
struct DeleteResponse {
    ok: bool,
    key_id: String,
    ordinal: u64,
    live_count: u64,
}

async fn delete(
    State(s): State<AppState>,
    Extension(ctx): Extension<Ctx>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let parsed: DeleteBody = match parse_json(&body, headers.get(header::CONTENT_TYPE)) {
        Ok(v) => v,
        Err(code) => {
            return err_response(&ctx, code, "请求体必须是包含 delete_token 的 JSON".into())
        }
    };
    if parsed.delete_token.trim().is_empty() {
        return err_response(&ctx, ErrorCode::TokenMalformed, "delete_token 为空".into());
    }
    match s.service.delete(parsed.delete_token.trim()) {
        Ok(rcpt) => {
            tracing::info!(request_id = %ctx.request_id, run_id = %s.run_id,
                key_id = %rcpt.key_id_b64, ordinal = rcpt.ordinal,
                live = rcpt.live_count, "删除成功");
            let mut resp = Json(DeleteResponse {
                ok: true,
                key_id: rcpt.key_id_b64,
                ordinal: rcpt.ordinal,
                live_count: rcpt.live_count,
            })
            .into_response();
            insert_ctx_headers(&mut resp, &ctx);
            resp
        }
        Err(e) => {
            let (_, code) = map_service_error(&e);
            tracing::warn!(request_id = %ctx.request_id, run_id = %s.run_id,
                code = ?code, error = %e, "删除被拒绝或失败");
            err_response(&ctx, code, e.to_string())
        }
    }
}

async fn not_found(Extension(ctx): Extension<Ctx>) -> Response {
    err_response(&ctx, ErrorCode::NotFound, "未知路由".into())
}

fn parse_json<T: serde::de::DeserializeOwned>(
    body: &Bytes,
    content_type: Option<&header::HeaderValue>,
) -> Result<T, ErrorCode> {
    if let Some(ct) = content_type {
        if let Ok(s) = ct.to_str() {
            if !s.to_lowercase().contains("application/json") {
                return Err(ErrorCode::InvalidRequest);
            }
        } else {
            return Err(ErrorCode::InvalidRequest);
        }
    } else {
        return Err(ErrorCode::InvalidRequest);
    }
    serde_json::from_slice(body).map_err(|_| ErrorCode::InvalidRequest)
}
