//! 错误分类与跨层错误契约。
//!
//! 四类需要可区分的问题（README“错误语义”详述）：
//!
//! | 类别 (`category`)       | HTTP | 含义                                       |
//! |-------------------------|------|--------------------------------------------|
//! | `input_error`           | 400  | 输入语言不合法（语法、标识符、JSON 等）    |
//! | `state_conflict`        | 409  | 输入本身合法，但与当前集合状态冲突；       |
//! |                         |      | 不可满足批次会携带负环证据                 |
//! | `resource_exhausted`    | 507  | 变量/约束数量或请求体超过服务端配额        |
//! | `computation_failure`   | 422  | 计算无法给出有界结果（整数加法溢出等）     |
//! | `not_found`             | 404  | 引用的命名资源不存在                       |
//! | `internal_error`        | 500  | 服务端内部故障（原则上不应出现）           |
//!
//! 每个错误都带 `run_id`，与响应头 `x-run-id`、服务端结构化日志一致，便于重放。

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde::Serialize;

use crate::model::ConflictDto;

/// 稳定、可断言的错误类别（见 README）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    InputError,
    StateConflict,
    ResourceExhausted,
    ComputationFailure,
    NotFound,
    InternalError,
}

impl ErrorKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::InputError => "input_error",
            ErrorKind::StateConflict => "state_conflict",
            ErrorKind::ResourceExhausted => "resource_exhausted",
            ErrorKind::ComputationFailure => "computation_failure",
            ErrorKind::NotFound => "not_found",
            ErrorKind::InternalError => "internal_error",
        }
    }

    pub fn http_status(self) -> StatusCode {
        match self {
            ErrorKind::InputError => StatusCode::BAD_REQUEST,
            ErrorKind::StateConflict => StatusCode::CONFLICT,
            ErrorKind::ResourceExhausted => StatusCode::INSUFFICIENT_STORAGE,
            ErrorKind::ComputationFailure => StatusCode::UNPROCESSABLE_ENTITY,
            ErrorKind::NotFound => StatusCode::NOT_FOUND,
            ErrorKind::InternalError => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }
}

/// 状态冲突的细节：批次使集合不可满足时携带负环证据。
#[derive(Debug, Clone, Serialize)]
pub struct ConflictDetails {
    /// 冲突原因：重名 / 集合不可满足。
    pub conflict: String,
    /// 不可满足时的严格负环；重名等其它状态冲突为 `None`。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub evidence: Option<ConflictDto>,
}

/// 统一错误体：`{"error":{"category":...,"message":...,...,"run_id":...}}`。
#[derive(Debug, Clone, Serialize)]
pub struct ApiError {
    category: ErrorKind,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    details: Option<ConflictDetails>,
    run_id: String,
}

impl ApiError {
    pub fn new(kind: ErrorKind, message: impl Into<String>, run_id: impl Into<String>) -> Self {
        ApiError {
            category: kind,
            message: message.into(),
            details: None,
            run_id: run_id.into(),
        }
    }

    /// 携带负环证据的状态冲突（不可满足批次）。
    pub fn unsat(
        message: impl Into<String>,
        evidence: ConflictDto,
        run_id: impl Into<String>,
    ) -> Self {
        ApiError {
            category: ErrorKind::StateConflict,
            message: message.into(),
            details: Some(ConflictDetails {
                conflict: "constraint_set_unsatisfiable".to_string(),
                evidence: Some(evidence),
            }),
            run_id: run_id.into(),
        }
    }

    pub fn input(message: impl Into<String>, run_id: &str) -> Self {
        Self::new(ErrorKind::InputError, message, run_id)
    }
    pub fn state(message: impl Into<String>, run_id: &str) -> Self {
        Self::new(ErrorKind::StateConflict, message, run_id)
    }
    pub fn resource(message: impl Into<String>, run_id: &str) -> Self {
        Self::new(ErrorKind::ResourceExhausted, message, run_id)
    }
    pub fn computation(message: impl Into<String>, run_id: &str) -> Self {
        Self::new(ErrorKind::ComputationFailure, message, run_id)
    }
    pub fn not_found(message: impl Into<String>, run_id: &str) -> Self {
        Self::new(ErrorKind::NotFound, message, run_id)
    }
    pub fn internal(message: impl Into<String>, run_id: &str) -> Self {
        Self::new(ErrorKind::InternalError, message, run_id)
    }

    pub fn category(&self) -> ErrorKind {
        self.category
    }

    /// 413（Payload Too Large）也归入资源耗尽：请求体本身超过限额。
    pub fn payload_too_large(message: impl Into<String>, run_id: &str) -> Self {
        Self::new(ErrorKind::ResourceExhausted, message, run_id)
    }
}

#[derive(Serialize)]
struct ErrorEnvelope {
    error: ApiError,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let run_id = self.run_id.clone();
        let status = self.category.http_status();
        let body = Json(ErrorEnvelope { error: self });
        let mut resp = (status, body).into_response();
        if let Ok(value) = run_id.parse() {
            resp.headers_mut().insert("x-run-id", value);
        }
        resp
    }
}
