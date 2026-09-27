//! HTTP 层：路由、运行编号注入、统一错误响应、JSON 编解码。

pub mod handlers;
pub mod types;

use std::sync::Arc;
use std::time::Instant;

use axum::{
    Router,
    extract::Request,
    http::{HeaderMap, Method, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};

use crate::error::{ErrorKind, FmError};
use crate::service::IndexService;

/// 共享应用状态。
pub type AppState = Arc<IndexService>;

/// 每请求自增序号，与 pid、纳秒时间共同构成可重放定位的运行编号。
static REQUEST_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

fn new_run_id() -> String {
    let seq = REQUEST_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("run-{}-{}-{:06}", std::process::id(), nanos, seq)
}

/// 成功响应包一层 `run_id`，错误也带同一个 `run_id`，便于用日志重放。
#[derive(serde::Serialize)]
pub(crate) struct Envelope<T: serde::Serialize> {
    pub run_id: String,
    pub data: T,
}

/// 统一错误体。
#[derive(serde::Serialize)]
pub struct ApiErrorBody {
    pub run_id: String,
    pub error: ErrorKind,
    pub message: String,
}

impl FmError {
    /// 转 HTTP 响应：按错误类别给状态码，并记录可重放日志。
    pub fn into_api_response(self, run_id: &str) -> Response {
        let kind = self.kind();
        match kind {
            ErrorKind::Corrupt | ErrorKind::ComputationFailed => {
                tracing::error!(run_id = %run_id, error = ?kind, message = %self, "请求处理失败");
            }
            _ => {
                tracing::debug!(run_id = %run_id, error = ?kind, message = %self, "请求被拒绝");
            }
        }
        let body = ApiErrorBody {
            run_id: run_id.to_string(),
            error: kind,
            message: self.to_string(),
        };
        let mut resp = (
            StatusCode::from_u16(kind.http_status()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
            axum::Json(body),
        )
            .into_response();
        resp.extensions_mut().insert(InternalResponse);
        resp
    }
}

/// 在请求扩展中携带的运行编号。
#[derive(Clone, Debug)]
pub struct RunId(pub String);

/// 标记“本响应由本服务 handler/错误转换产生”，中间件据此跳过框架级改写。
#[derive(Clone, Debug)]
pub struct InternalResponse;

/// 中间件：为每个请求生成/接纳运行编号，做访问日志，并统一框架级错误的响应格式。
async fn run_middleware(
    headers: HeaderMap,
    method: Method,
    mut req: Request,
    next: Next,
) -> Response {
    let run_id = headers
        .get("x-run-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .map(|s| s.to_string())
        .unwrap_or_else(new_run_id);

    let path = req.uri().path().to_string();
    let started = Instant::now();
    tracing::info!(run_id = %run_id, method = %method, path = %path, "请求开始");

    req.extensions_mut().insert(RunId(run_id.clone()));

    let resp = next.run(req).await;
    let status = resp.status();
    // 我们自己的响应（无论成功信封还是 FmError 错误体）都会打 InternalResponse 标记；
    // 没有标记的 404/405/413 才是框架层产生的，需要统一成错误契约。
    let internal = resp.extensions().get::<InternalResponse>().is_some();

    if !internal && status == StatusCode::PAYLOAD_TOO_LARGE {
        return FmError::resource_exhausted("请求体超过服务端 max_body_bytes 限制")
            .into_api_response(&run_id);
    }
    if !internal && status == StatusCode::NOT_FOUND {
        return FmError::not_found("路由不存在").into_api_response(&run_id);
    }
    if !internal && status == StatusCode::METHOD_NOT_ALLOWED {
        return FmError::not_found("HTTP 方法不允许").into_api_response(&run_id);
    }

    tracing::info!(
        run_id = %run_id,
        status = status.as_u16(),
        elapsed_us = started.elapsed().as_micros() as u64,
        "请求结束"
    );
    resp
}

/// 构造完整应用路由。
pub fn app(state: AppState, max_body_bytes: usize) -> Router {
    let v1 = Router::new()
        .route("/health", get(handlers::health))
        .route(
            "/indexes",
            get(handlers::list_indexes).post(handlers::create_index),
        )
        .route(
            "/indexes/:name",
            get(handlers::get_index).delete(handlers::delete_index),
        )
        .route("/indexes/:name/load", post(handlers::load_index))
        .route(
            "/indexes/:name/search",
            get(handlers::search_get).post(handlers::search_post),
        )
        .route("/indexes/:name/count", get(handlers::count_get))
        .route(
            "/indexes/:name/verify",
            get(handlers::verify_get).post(handlers::verify_post),
        );

    Router::new()
        .nest("/v1", v1)
        .layer(middleware::from_fn(run_middleware))
        .layer(axum::extract::DefaultBodyLimit::max(max_body_bytes))
        .with_state(state)
}
