//! 请求身份中间件：为每个请求分配/继承一个 request id，注入扩展与响应头，
//! 并在错误响应体中回填该身份。

use axum::extract::Request;
use axum::http::{HeaderName, HeaderValue};
use axum::middleware::Next;
use axum::response::Response;
use uuid::Uuid;

use crate::handlers::patch_error_id;

/// 扩展中的请求 id 载体（处理器通过 [`ReqId`] 提取器读取）。
#[derive(Debug, Clone)]
pub struct ReqId(pub String);

/// 请求 id 请求/响应头名。
pub const REQ_HEADER: &str = "x-request-id";

/// Axum 中间件函数：在 `router().layer(middleware::from_fn(mw))` 中使用。
pub async fn mw(mut req: Request, next: Next) -> Response {
    let id = req
        .headers()
        .get(REQ_HEADER)
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 128 && s.is_ascii())
        .map(|s| s.to_string())
        .unwrap_or_else(|| Uuid::new_v4().to_string());

    req.extensions_mut().insert(ReqId(id.clone()));

    let mut resp = next.run(req).await;
    if let Ok(v) = HeaderValue::from_str(&id) {
        resp.headers_mut()
            .insert(HeaderName::from_static("x-request-id"), v);
    }
    // 把错误信封里的占位 request_id 回填为真实身份（成功响应原样透传）。
    patch_error_id(id, resp).await
}
