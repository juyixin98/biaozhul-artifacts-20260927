//! HTTP 错误映射：每类失败都有稳定的 `error_code` 与合适的 HTTP 状态码；
//! 5xx 仅用于存储/内部异常，绝不把未知状态返回为成功。

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

use cuckoo_core::FailureKind;
use cuckoo_persist::PersistError;

#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub code: &'static str,
    pub message: String,
}

impl ApiError {
    pub fn bad_request(msg: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            code: "BAD_REQUEST",
            message: msg.into(),
        }
    }

    pub fn not_found(msg: impl Into<String>) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            code: "NOT_FOUND",
            message: msg.into(),
        }
    }

    pub fn internal(msg: impl Into<String>) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "INTERNAL",
            message: msg.into(),
        }
    }
}

impl From<PersistError> for ApiError {
    fn from(e: PersistError) -> Self {
        match &e {
            PersistError::Core(core) => {
                let (status, code) = match core.kind() {
                    FailureKind::InvalidParams => {
                        (StatusCode::BAD_REQUEST, FailureKind::InvalidParams.as_str())
                    }
                    FailureKind::FilterFull => {
                        // 容量耗尽是可预期的业务失败，用 507 表达。
                        (StatusCode::INSUFFICIENT_STORAGE, FailureKind::FilterFull.as_str())
                    }
                    FailureKind::NotPresent => {
                        (StatusCode::NOT_FOUND, FailureKind::NotPresent.as_str())
                    }
                    FailureKind::InvalidCredential => (
                        StatusCode::FORBIDDEN,
                        FailureKind::InvalidCredential.as_str(),
                    ),
                    FailureKind::CredentialExhausted => (
                        StatusCode::CONFLICT,
                        FailureKind::CredentialExhausted.as_str(),
                    ),
                };
                ApiError {
                    status,
                    code,
                    message: e.to_string(),
                }
            }
            PersistError::Storage(_) => ApiError {
                status: StatusCode::INTERNAL_SERVER_ERROR,
                code: "STORAGE_ERROR",
                message: e.to_string(),
            },
            PersistError::Init(_) => ApiError {
                status: StatusCode::INTERNAL_SERVER_ERROR,
                code: "INIT_ERROR",
                message: e.to_string(),
            },
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = Json(json!({
            "ok": false,
            "error_code": self.code,
            "error": self.message,
        }));
        (self.status, body).into_response()
    }
}
