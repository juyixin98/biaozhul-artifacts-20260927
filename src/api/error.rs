//! 错误到 HTTP 状态码的映射：400 请求结构/输入非法；422 证据不成立；
//! 500 内部错误。可达性本身的 reachable/unreachable/inconclusive 是 200 业务结论，
//! 绝不用 4xx/5xx 表达。

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;

use super::dto::{ErrorBody, IssueDto};

#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub code: String,
    pub message: String,
    pub request_id: String,
    pub issues: Vec<crate::input::Issue>,
    pub details: Option<serde_json::Value>,
}

impl ApiError {
    pub fn bad_request(code: impl Into<String>, msg: impl Into<String>, rid: &str) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            code: code.into(),
            message: msg.into(),
            request_id: rid.to_string(),
            issues: Vec::new(),
            details: None,
        }
    }

    pub fn from_input(err: crate::input::InputError, rid: &str) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            code: err.primary_code().to_string(),
            message: describe_input(&err),
            request_id: rid.to_string(),
            issues: err.issues,
            details: None,
        }
    }

    pub fn unprocessable(code: impl Into<String>, msg: impl Into<String>, rid: &str) -> Self {
        ApiError {
            status: StatusCode::UNPROCESSABLE_ENTITY,
            code: code.into(),
            message: msg.into(),
            request_id: rid.to_string(),
            issues: Vec::new(),
            details: None,
        }
    }

    /// 附带结构化失败细节（如证据验证里被阻断的那一步）。
    pub fn with_step(self, step: crate::verify::InvalidStep) -> Self {
        ApiError {
            details: serde_json::to_value(step).ok(),
            ..self
        }
    }

    pub fn internal(msg: impl Into<String>, rid: &str) -> Self {
        ApiError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "internal_error".into(),
            message: msg.into(),
            request_id: rid.to_string(),
            issues: Vec::new(),
            details: None,
        }
    }
}

fn describe_input(err: &crate::input::InputError) -> String {
    err.issues
        .first()
        .map(|i| i.message.clone())
        .unwrap_or_else(|| "invalid net input".to_string())
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = ErrorBody {
            error: self.code,
            message: self.message,
            request_id: self.request_id,
            issues: self
                .issues
                .into_iter()
                .map(|i| IssueDto {
                    code: i.code.to_string(),
                    message: i.message,
                    location: i.location,
                })
                .collect(),
            details: self.details,
        };
        (self.status, Json(body)).into_response()
    }
}
