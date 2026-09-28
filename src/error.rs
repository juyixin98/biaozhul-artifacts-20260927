//! 统一的错误类型：输入错误 / 状态冲突属于 4xx 客户端错误；
//! 计算失败属于 5xx；资源耗尽不是错误，判定结果为 `unknown`。

use serde::Serialize;

/// 可区分的错误大类，对应 HTTP 状态码与 README 中记载的错误语义。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCategory {
    /// 请求本身不合法（畸形 JSON、字段缺失、值非法）。HTTP 400。
    InputError,
    /// 输入可解析但描述了一个自相矛盾的系统（重名、悬空引用、字母表不一致等）。HTTP 409。
    StateConflict,
    /// 请求超过服务端体量限制（body 过大、硬性上限）。HTTP 413。
    PayloadTooLarge,
    /// 求解器内部计算失败。HTTP 500。
    ComputationFailure,
}

impl ErrorCategory {
    #[must_use]
    pub fn http_status(self) -> u16 {
        match self {
            ErrorCategory::InputError => 400,
            ErrorCategory::StateConflict => 409,
            ErrorCategory::PayloadTooLarge => 413,
            ErrorCategory::ComputationFailure => 500,
        }
    }
}

/// 结构化的错误代码，测试会精确断言具体类别，而不只是“接口返回了错误”。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    // ---- input_error ----
    MalformedJson,
    InvalidRequestShape,
    EmptyObservableAlphabet,
    InvalidLimitValue,
    MissingField,
    InvalidActionName,
    EmptyStateName,
    EmptyEdgeId,
    // ---- state_conflict ----
    DuplicateState,
    DuplicateEdgeId,
    UnknownState,
    UnknownAction,
    DuplicateObservableAction,
    DuplicateHiddenAction,
    ObservableHiddenOverlap,
    InitialStatesEmpty,
    NoInitialState,
    // ---- payload_too_large ----
    BodyTooLarge,
    HardLimitExceeded,
    // ---- computation_failure ----
    SolverFailure,
}

impl ErrorCode {
    #[must_use]
    pub fn category(self) -> ErrorCategory {
        use ErrorCode::*;
        match self {
            MalformedJson | InvalidRequestShape | EmptyObservableAlphabet | InvalidLimitValue
            | MissingField | InvalidActionName | EmptyStateName | EmptyEdgeId
            | DuplicateObservableAction => ErrorCategory::InputError,
            DuplicateState | DuplicateEdgeId | UnknownState | UnknownAction
            | DuplicateHiddenAction | ObservableHiddenOverlap | InitialStatesEmpty
            | NoInitialState => ErrorCategory::StateConflict,
            BodyTooLarge | HardLimitExceeded => ErrorCategory::PayloadTooLarge,
            SolverFailure => ErrorCategory::ComputationFailure,
        }
    }
}

/// 模块之间流转的唯一错误类型。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ApiError {
    pub category: ErrorCategory,
    pub code: ErrorCode,
    /// 人类可读的说明，稳定的机器可读片段放在 `details`。
    pub message: String,
    /// 出错的具体位置（字段路径、状态名、动作名等），便于复现定位。
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub details: Vec<String>,
}

/// 库内部统一的 Result 别名。
pub type Result<T> = std::result::Result<T, ApiError>;

impl ApiError {
    #[must_use]
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            category: code.category(),
            code,
            message: message.into(),
            details: Vec::new(),
        }
    }

    #[must_use]
    pub fn with_detail(mut self, detail: impl Into<String>) -> Self {
        self.details.push(detail.into());
        self
    }

    #[must_use]
    pub fn with_details(mut self, details: Vec<String>) -> Self {
        self.details = details;
        self
    }
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}: {}", self.code, self.message)?;
        if !self.details.is_empty() {
            write!(f, " [{}]", self.details.join(", "))?;
        }
        Ok(())
    }
}

impl std::error::Error for ApiError {}
