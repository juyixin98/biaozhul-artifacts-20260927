#![allow(clippy::result_large_err)]
//! 后端接口装配：路由、请求标识中间件、请求体限制、追踪层。

pub mod dto;
pub mod error;
pub mod handlers;

use std::sync::Arc;

use axum::middleware::{self, Next};
use axum::response::Response;
use axum::routing::{get, post};
use axum::{Extension, Router};
use tower::limit::ConcurrencyLimitLayer;
use tower_http::trace::TraceLayer;

use crate::config::Config;
use crate::request_id::{self};
use handlers::AppState;

const REQUEST_ID_HEADER: &str = "x-request-id";

/// 绑定 request-id 任务局部变量、回写响应头。
async fn request_id_middleware(
    headers: axum::http::HeaderMap,
    mut req: axum::extract::Request,
    next: Next,
) -> Response {
    let rid = request_id::extract_request_id(&headers);
    req.extensions_mut().insert(RequestId(rid.clone()));
    let response = request_id::scope(rid.clone(), next.run(req)).await;
    attach(response, &rid)
}

/// 透传 request id 的扩展项（处理器也可从扩展读取；当前处理器直接用请求头）。
#[derive(Clone, Debug)]
pub struct RequestId(pub String);

fn attach(mut response: Response, rid: &str) -> Response {
    if let Ok(v) = axum::http::HeaderValue::from_str(rid) {
        response.headers_mut().insert(REQUEST_ID_HEADER, v);
    }
    response
}

pub fn build_router(config: Arc<Config>) -> Router {
    let state = AppState {
        config: config.clone(),
    };

    let json_routes = Router::new()
        .route("/api/v1/reachability", post(handlers::reachability))
        .route("/api/v1/invariants", post(handlers::invariants))
        .route("/api/v1/verify/firing", post(handlers::verify_firing))
        .route(
            "/api/v1/verify/invariant",
            post(handlers::verify_invariant),
        );

    Router::new()
        .route("/health", get(handlers::health))
        .merge(json_routes)
        .layer(middleware::from_fn(request_id_middleware))
        .layer(Extension(config.body_limit_bytes))
        .layer(axum::extract::DefaultBodyLimit::max(config.body_limit_bytes))
        // 防止异常大并发把求解线程池打满；并发上限取一个保守常量。
        .layer(ConcurrencyLimitLayer::new(256))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}
