//! 请求标识中间件：为每个请求注入 [`RequestId`] 扩展并在响应头回写。

use axum::extract::Request;
use axum::http::HeaderValue;
use axum::middleware::Next;
use axum::response::Response;

use super::diag::RequestId;

/// Axum 中间件：读取/生成 `x-request-id`，注入扩展并回写响应头。
pub async fn layer(mut req: Request, next: Next) -> Response {
    let incoming = req
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok());
    let rid = RequestId::from_header(incoming);

    let mut resp = {
        req.extensions_mut().insert(rid.clone());
        next.run(req).await
    };
    if let Ok(v) = HeaderValue::from_str(rid.as_str()) {
        resp.headers_mut().insert("x-request-id", v);
    }
    resp
}
