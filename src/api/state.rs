//! Shared server state and error-to-response mapping.

use std::collections::HashMap;
use std::sync::Mutex;

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;

use crate::config::Config;
use crate::diag::Diag;
use crate::kernel::{BddManager, KernelError};

/// Process-wide state: the manager table and resolved configuration.
pub struct AppState {
    pub managers: Mutex<HashMap<u64, BddManager>>,
    pub config: Config,
}

impl AppState {
    pub fn new(config: Config) -> Self {
        AppState {
            managers: Mutex::new(HashMap::new()),
            config,
        }
    }
}

/// Any API failure, carrying enough to produce the categorized error body.
#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub kind: String,
    pub message: String,
    pub request_id: String,
    pub state: serde_json::Value,
}

impl ApiError {
    pub fn bad_request(request_id: &str, kind: &str, message: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            kind: kind.into(),
            message: message.into(),
            request_id: request_id.into(),
            state: serde_json::json!({}),
        }
    }

    pub fn not_found(request_id: &str, message: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::NOT_FOUND,
            kind: "unknown-manager".into(),
            message: message.into(),
            request_id: request_id.into(),
            state: serde_json::json!({}),
        }
    }

    pub fn with_state(mut self, state: serde_json::Value) -> Self {
        self.state = state;
        self
    }
}

impl From<KernelError> for ApiError {
    fn from(e: KernelError) -> Self {
        // Request id is attached at the handler boundary via `tag`.
        ApiError {
            status: StatusCode::UNPROCESSABLE_ENTITY,
            kind: e.kind.as_code().into(),
            message: e.detail,
            request_id: String::new(),
            state: serde_json::json!({}),
        }
    }
}

impl ApiError {
    pub fn tag(mut self, request_id: &str) -> Self {
        self.request_id = request_id.into();
        self
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let diag = Diag::error(&self.request_id, &self.kind, self.message.clone())
            .with_state(self.state.clone());
        tracing::warn!(
            request_id = %self.request_id,
            kind = %self.kind,
            state = %self.state,
            "request rejected: {}",
            self.message
        );
        (
            self.status,
            Json(crate::api::dto::ErrorBody {
                error: crate::api::dto::ErrorPayload {
                    kind: self.kind,
                    message: self.message,
                },
                diag,
            }),
        )
            .into_response()
    }
}

pub type ApiResult<T> = Result<T, ApiError>;

/// Convenience: parse a Boolean expression, mapping failures to the API
/// category `invalid-expr`.
pub fn parse_expr(request_id: &str, src: &str) -> ApiResult<crate::lang::Expr> {
    crate::lang::parse(src).map_err(|e| {
        ApiError::bad_request(request_id, "invalid-expr", format!("{e}"))
            .with_state(serde_json::json!({"span": {"start": e.span.start, "end": e.span.end}}))
    })
}

/// Parse the `op` field into a kernel operation.
pub fn parse_op(request_id: &str, raw: &str) -> ApiResult<crate::kernel::Op> {
    use crate::kernel::Op::*;
    let op = match raw {
        "and" | "&&" | "&" => And,
        "or" | "||" | "|" => Or,
        "xor" | "^" => Xor,
        "implies" | "->" => Implies,
        "equiv" | "<->" | "=" => Equiv,
        other => {
            return Err(ApiError::bad_request(
                request_id,
                "unknown-op",
                format!("unknown operator {other:?}"),
            ));
        }
    };
    Ok(op)
}
