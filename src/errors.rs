//! Stable, machine-readable failure categories.
//!
//! Every error has a fixed string code (appears in HTTP JSON bodies, log
//! records and test assertions) and maps to an HTTP status. Unknown or failed
//! operations are **never** reported as success.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::Serialize;

/// Application-wide error type.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AppError {
    /// Update targeted a coordinate pair not present in the registered tables.
    /// Contains `(x, y)`.
    UnregisteredCoord(i64, i64),
    /// Same coordinate appeared more than once inside one batch. Contains `(x, y)`.
    DuplicateInBatch(i64, i64),
    /// A submitted batch contained zero updates (nothing would be published).
    EmptyBatch,
    /// Point total would leave the `i64` range after this batch.
    /// Contains human-readable detail: `"x,y,old+delta"`.
    Overflow(String),
    /// Coordinate tables were never registered yet (store has no versions).
    NotInitialized,
    /// Re-register request supplied an empty coordinate set on either axis.
    /// Contains the axis name.
    EmptyAxis(&'static str),
    /// Requested version id does not exist.
    UnknownVersion(u64),
    /// Malformed HTTP request (bad JSON body, bad query parameter, ...).
    BadRequest(String),
    /// Request body exceeded the configured size limit.
    PayloadTooLarge(String),
    /// Persistence failure (I/O, corrupt log, hash mismatch, ...).
    Persistence(String),
}

impl AppError {
    /// Stable snake_case code used by clients and tests.
    pub fn code(&self) -> &'static str {
        match self {
            AppError::UnregisteredCoord(_, _) => "unregistered_coordinate",
            AppError::DuplicateInBatch(_, _) => "duplicate_in_batch",
            AppError::EmptyBatch => "empty_batch",
            AppError::Overflow(_) => "overflow",
            AppError::NotInitialized => "not_initialized",
            AppError::EmptyAxis(_) => "empty_axis",
            AppError::UnknownVersion(_) => "unknown_version",
            AppError::BadRequest(_) => "bad_request",
            AppError::PayloadTooLarge(_) => "payload_too_large",
            AppError::Persistence(_) => "persistence_error",
        }
    }

    fn message(&self) -> String {
        match self {
            AppError::UnregisteredCoord(x, y) => {
                format!("coordinate ({x}, {y}) is not registered; updates to unregistered coordinates are rejected")
            }
            AppError::DuplicateInBatch(x, y) => {
                format!("coordinate ({x}, {y}) appears more than once in the same batch")
            }
            AppError::EmptyBatch => "batch must contain at least one update".to_string(),
            AppError::Overflow(detail) => {
                format!("accumulated point total overflows i64: {detail}")
            }
            AppError::NotInitialized => {
                "coordinate tables are not registered; POST /admin/register first".to_string()
            }
            AppError::EmptyAxis(axis) => {
                format!("registered coordinate set for axis {axis} is empty")
            }
            AppError::UnknownVersion(v) => format!("version {v} does not exist"),
            AppError::BadRequest(m) => format!("malformed request: {m}"),
            AppError::PayloadTooLarge(m) => format!("request body too large: {m}"),
            AppError::Persistence(m) => format!("persistence failure: {m}"),
        }
    }

    fn status(&self) -> StatusCode {
        match self {
            AppError::UnregisteredCoord(_, _)
            | AppError::DuplicateInBatch(_, _)
            | AppError::EmptyBatch
            | AppError::Overflow(_)
            | AppError::EmptyAxis(_)
            | AppError::BadRequest(_) => StatusCode::UNPROCESSABLE_ENTITY,
            AppError::PayloadTooLarge(_) => StatusCode::PAYLOAD_TOO_LARGE,
            AppError::NotInitialized => StatusCode::PRECONDITION_FAILED,
            AppError::UnknownVersion(_) => StatusCode::NOT_FOUND,
            AppError::Persistence(_) => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    /// Attach the run/request identity so failure logs can be correlated.
    pub fn with_rid(self, request_id: String) -> ApiError {
        ApiError {
            inner: self,
            request_id,
        }
    }
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message())
    }
}

impl std::error::Error for AppError {}

/// An [`AppError`] decorated with the request id; this is the type handlers
/// return so the JSON body always carries the correlation id.
#[derive(Debug)]
pub struct ApiError {
    pub inner: AppError,
    pub request_id: String,
}

#[derive(Serialize)]
struct ErrorBody<'a> {
    ok: bool,
    error_code: &'static str,
    error: String,
    request_id: &'a str,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = ErrorBody {
            ok: false,
            error_code: self.inner.code(),
            error: self.inner.message(),
            request_id: &self.request_id,
        };
        tracing::warn!(
            "request failed request_id={} error_code={} error={}",
            self.request_id,
            self.inner.code(),
            body.error
        );
        (self.inner.status(), Json(body)).into_response()
    }
}

/// Convenient handler return type.
pub type ApiResult<T> = Result<T, ApiError>;
