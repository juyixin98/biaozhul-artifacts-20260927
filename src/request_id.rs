//! 请求关联标识：优先采用客户端 `X-Request-Id`，否则生成 UUIDv4；
//! 通过任务局部变量在内核/解析层之外的任意代码处读取，写入日志与响应。

use axum::http::HeaderMap;
use uuid::Uuid;

tokio::task_local! {
    static REQUEST_ID: String;
}

/// 在请求 future 上绑定 request id（中间件调用）。
pub async fn scope<F, R>(rid: String, f: F) -> R
where
    F: std::future::Future<Output = R>,
{
    REQUEST_ID.scope(rid, f).await
}

/// 取当前任务绑定的 request id（未绑定时给出占位值，绝不 panic）。
pub fn current_request_id() -> String {
    REQUEST_ID
        .try_with(|s| s.clone())
        .unwrap_or_else(|_| "no-request-id".to_string())
}

pub fn extract_request_id(headers: &HeaderMap) -> String {
    headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .unwrap_or_else(|| Uuid::new_v4().to_string())
}
