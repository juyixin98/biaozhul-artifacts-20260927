//! Application-wide error type carrying a stable machine code, an HTTP status,
//! and a human-readable message. Every failure path in the API maps here so
//! responses and logs explain *what* failed and *why*.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

use crate::erasure::CodeError;
use crate::manifest::ManifestError;

#[derive(Debug)]
pub struct AppError {
    pub status: StatusCode,
    pub code: &'static str,
    pub message: String,
    /// Optional structured detail (e.g. missing vs corrupt shard lists).
    pub detail: serde_json::Value,
}

impl AppError {
    pub fn new(status: StatusCode, code: &'static str, message: impl Into<String>) -> Self {
        Self {
            status,
            code,
            message: message.into(),
            detail: json!({}),
        }
    }

    pub fn with_detail(mut self, detail: serde_json::Value) -> Self {
        self.detail = detail;
        self
    }

    pub fn bad_request(code: &'static str, msg: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, code, msg)
    }

    pub fn not_found(msg: impl Into<String>) -> Self {
        Self::new(StatusCode::NOT_FOUND, "OBJECT_NOT_FOUND", msg)
    }

    pub fn payload_too_large(msg: impl Into<String>) -> Self {
        Self::new(StatusCode::PAYLOAD_TOO_LARGE, "PAYLOAD_TOO_LARGE", msg)
    }

    pub fn internal(msg: impl Into<String>) -> Self {
        Self::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "INTERNAL_ERROR",
            msg,
        )
    }

    pub fn conflict(code: &'static str, msg: impl Into<String>) -> Self {
        Self::new(StatusCode::CONFLICT, code, msg)
    }
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}
impl std::error::Error for AppError {}

impl From<CodeError> for AppError {
    fn from(e: CodeError) -> Self {
        let status = match e {
            CodeError::NotEnoughShards { .. } => StatusCode::CONFLICT,
            CodeError::BadParams { .. }
            | CodeError::DuplicateOrInvalidIndex { .. }
            | CodeError::ShardLengthMismatch { .. } => StatusCode::BAD_REQUEST,
            CodeError::SingularSystem => StatusCode::INTERNAL_SERVER_ERROR,
        };
        AppError::new(status, e.code(), e.to_string())
    }
}

impl From<ManifestError> for AppError {
    fn from(e: ManifestError) -> Self {
        let status = match e {
            ManifestError::UnsupportedVersion { .. } | ManifestError::UnsupportedAlgorithm { .. } => {
                StatusCode::UNPROCESSABLE_ENTITY
            }
            _ => StatusCode::UNPROCESSABLE_ENTITY,
        };
        AppError::new(status, e.code(), e.to_string())
    }
}

impl From<std::io::Error> for AppError {
    fn from(e: std::io::Error) -> Self {
        match e.kind() {
            std::io::ErrorKind::NotFound => {
                AppError::not_found(format!("file not found: {e}"))
            }
            std::io::ErrorKind::PermissionDenied => AppError::internal(format!(
                "filesystem permission denied: {e}"
            )),
            _ => AppError::internal(format!("filesystem error: {e}")),
        }
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let body = json!({
            "ok": false,
            "error": {
                "code": self.code,
                "message": self.message,
                "detail": self.detail,
            }
        });
        (self.status, Json(body)).into_response()
    }
}

pub type AppResult<T> = Result<T, AppError>;
