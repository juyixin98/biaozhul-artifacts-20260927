//! Typed API error: stable category codes and HTTP status mapping.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::Serialize;

use wm_core::WmError;
use wm_format::FormatError;
use wm_store::StoreError;

use crate::request_id::RequestId;

/// Error envelope returned for every failed request.
#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub ok: bool,
    pub request_id: String,
    pub error: ErrorDetail,
}

#[derive(Debug, Serialize)]
pub struct ErrorDetail {
    /// Stable snake_case category, e.g. `K_OUT_OF_BOUNDS`.
    pub kind: String,
    /// Human-readable explanation.
    pub message: String,
}

/// Application error carrying status plus a stable category.
#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub kind: String,
    pub message: String,
    pub request_id: Option<String>,
}

impl ApiError {
    pub fn new(status: StatusCode, kind: &str, message: impl Into<String>) -> Self {
        Self {
            status,
            kind: kind.to_string(),
            message: message.into(),
            request_id: None,
        }
    }

    fn bad_request(kind: &str, message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, kind, message)
    }

    /// Attach the correlated request identity before producing a response.
    #[must_use]
    pub fn with_request_id(mut self, id: &str) -> Self {
        self.request_id = Some(id.to_string());
        self
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let request_id = self.request_id.unwrap_or_else(RequestId::generated);
        tracing::warn!(
            request_id = %request_id,
            status = %self.status,
            kind = %self.kind,
            message = %self.message,
            "request failed"
        );
        (
            self.status,
            Json(ErrorBody {
                ok: false,
                request_id,
                error: ErrorDetail {
                    kind: self.kind,
                    message: self.message,
                },
            }),
        )
            .into_response()
    }
}

impl From<WmError> for ApiError {
    fn from(e: WmError) -> Self {
        let status = match e {
            WmError::EmptyValues => StatusCode::BAD_REQUEST,
            WmError::EmptyRange { .. } | WmError::KOutOfBounds { .. } => {
                StatusCode::UNPROCESSABLE_ENTITY
            }
            WmError::RangeOutOfBounds { .. } => StatusCode::UNPROCESSABLE_ENTITY,
        };
        ApiError::new(status, e.kind(), e.to_string())
    }
}

impl From<StoreError> for ApiError {
    fn from(e: StoreError) -> Self {
        match e {
            StoreError::InvalidName { .. } => {
                ApiError::bad_request("INVALID_INDEX_NAME", e.to_string())
            }
            StoreError::NotFound { .. } => {
                ApiError::new(StatusCode::NOT_FOUND, "INDEX_NOT_FOUND", e.to_string())
            }
            StoreError::AlreadyExists { .. } => {
                ApiError::new(StatusCode::CONFLICT, "INDEX_ALREADY_EXISTS", e.to_string())
            }
            StoreError::Io { .. } => ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "STORAGE_IO_ERROR",
                e.to_string(),
            ),
            StoreError::Corrupt { source, .. } => source.into(),
            StoreError::Kernel { source, .. } => source.into(),
        }
    }
}

impl From<FormatError> for ApiError {
    fn from(e: FormatError) -> Self {
        let kind = match &e {
            FormatError::TooShort(_) => "INDEX_FILE_TOO_SHORT",
            FormatError::BadMagic => "INDEX_FILE_BAD_MAGIC",
            FormatError::UnsupportedVersion { .. } => "INDEX_FILE_UNSUPPORTED_VERSION",
            FormatError::LengthMismatch { .. } => "INDEX_FILE_LENGTH_MISMATCH",
            FormatError::ChecksumMismatch { .. } => "INDEX_FILE_CHECKSUM_MISMATCH",
            FormatError::Truncated { .. } => "INDEX_FILE_TRUNCATED",
            FormatError::TooLarge { .. } => "INDEX_FILE_FIELD_TOO_LARGE",
            FormatError::InvalidStructure(_) => "INDEX_FILE_INVALID_STRUCTURE",
        };
        // Persisted data that cannot be decoded is a server-side data
        // integrity problem, not a malformed client request.
        ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, kind, e.to_string())
    }
}

/// Convert JSON body parse failures into a specific, non-leaky rejection.
pub fn json_rejection(err: String) -> ApiError {
    ApiError::bad_request(
        "INVALID_JSON",
        format!("request body is not valid for the endpoint schema: {err}"),
    )
}
