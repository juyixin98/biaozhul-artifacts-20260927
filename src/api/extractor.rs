//! JSON 提取器：把 axum 内置的“非 JSON 形态拒绝”统一成 `BAD_JSON`（400），
//! 错误信封已带 request_id/run_id；请求过大映射为 `PAYLOAD_TOO_LARGE`（413）。
//!
//! axum-core 0.4 的 `FromRequest` 由 `#[async_trait]` 定义，手动实现必须同样标注。

use axum::async_trait;
use axum::body::{Body, Bytes};
use axum::extract::FromRequest;
use axum::http::{Request, StatusCode};
use axum::response::Response;

use super::dto::ErrorBody;
use crate::error::CoreError;

pub struct ApiJson<T>(pub T);

#[async_trait]
impl<S, T> FromRequest<S, Body> for ApiJson<T>
where
    S: Send + Sync,
    T: serde::de::DeserializeOwned,
{
    type Rejection = Response;

    async fn from_request(req: Request<Body>, state: &S) -> Result<Self, Self::Rejection> {
        let request_id = req
            .extensions()
            .get::<String>()
            .cloned()
            .unwrap_or_else(|| "unknown-request".to_string());
        let run_id = req
            .extensions()
            .get::<super::routes::RunIdExt>()
            .map(|r| r.0.clone())
            .unwrap_or_else(|| "unknown-run".to_string());

        // 先整体取字节，便于按长度限制分类，而不是让 serde 看到截断流。
        let bytes = match Bytes::from_request(req, state).await {
            Ok(b) => b,
            Err(rej) => {
                let status =
                    StatusCode::from_u16(rej.status().as_u16()).unwrap_or(StatusCode::BAD_REQUEST);
                let code = if status == StatusCode::PAYLOAD_TOO_LARGE {
                    "PAYLOAD_TOO_LARGE"
                } else {
                    "BAD_JSON"
                };
                return Err(super::routes::error_response(
                    status,
                    CoreError::BadJson(format!("body rejected: {status}")),
                    request_id,
                    run_id,
                    Some(code),
                ));
            }
        };
        let value: T = match serde_json::from_slice(&bytes) {
            Ok(v) => v,
            Err(e) => {
                return Err(super::routes::error_response(
                    StatusCode::BAD_REQUEST,
                    CoreError::BadJson(e.to_string()),
                    request_id,
                    run_id,
                    None,
                ));
            }
        };
        Ok(ApiJson(value))
    }
}

#[allow(dead_code)]
fn _assert_error_body_shape(_: &ErrorBody) {}
