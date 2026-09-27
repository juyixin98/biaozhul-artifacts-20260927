//! 跨模块统一错误契约。
//!
//! 所有模块（编码内核、持久化、HTTP 层）只产生 [`FmError`]，
//! 每个错误都带一个机器可判别的 [`ErrorKind`]，HTTP 响应与测试断言都基于该判别值：
//!
//! | kind                 | 含义                                       | HTTP |
//! |----------------------|--------------------------------------------|------|
//! | `invalid_input`      | 输入参数非法（空文本、采样步长 0、坏 base64 等） | 400  |
//! | `not_found`          | 索引/文件不存在                             | 404  |
//! | `state_conflict`     | 同名索引已存在、状态冲突                     | 409  |
//! | `resource_exhausted` | 文本过长、结果过多、请求体过大               | 413  |
//! | `corrupt`            | 持久化文件缺失/截断/校验和不符               | 500  |
//! | `computation_failed` | 内部不变量被破坏、其它 I/O 与计算错误         | 500  |

use std::io;

/// 错误大类，可序列化进 HTTP 错误体，测试直接按字符串断言。
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    InvalidInput,
    NotFound,
    StateConflict,
    ResourceExhausted,
    Corrupt,
    ComputationFailed,
}

impl ErrorKind {
    /// 对应 HTTP 状态码。
    pub fn http_status(self) -> u16 {
        match self {
            ErrorKind::InvalidInput => 400,
            ErrorKind::NotFound => 404,
            ErrorKind::StateConflict => 409,
            ErrorKind::ResourceExhausted => 413,
            ErrorKind::Corrupt | ErrorKind::ComputationFailed => 500,
        }
    }
}

/// 全服务唯一错误类型。
#[derive(Debug, thiserror::Error)]
pub enum FmError {
    #[error("invalid input: {detail}")]
    InvalidInput { detail: String },

    #[error("not found: {detail}")]
    NotFound { detail: String },

    #[error("state conflict: {detail}")]
    StateConflict { detail: String },

    #[error("resource exhausted: {detail}")]
    ResourceExhausted { detail: String },

    /// 持久化数据损坏：携带 index 名（可空）与具体细节。
    #[error("corrupt index data ({detail})")]
    Corrupt { detail: String },

    #[error("computation failed: {detail}")]
    ComputationFailed { detail: String },
}

impl FmError {
    pub fn invalid_input(detail: impl Into<String>) -> Self {
        FmError::InvalidInput {
            detail: detail.into(),
        }
    }
    pub fn not_found(detail: impl Into<String>) -> Self {
        FmError::NotFound {
            detail: detail.into(),
        }
    }
    pub fn state_conflict(detail: impl Into<String>) -> Self {
        FmError::StateConflict {
            detail: detail.into(),
        }
    }
    pub fn resource_exhausted(detail: impl Into<String>) -> Self {
        FmError::ResourceExhausted {
            detail: detail.into(),
        }
    }
    pub fn corrupt(detail: impl Into<String>) -> Self {
        FmError::Corrupt {
            detail: detail.into(),
        }
    }
    pub fn computation_failed(detail: impl Into<String>) -> Self {
        FmError::ComputationFailed {
            detail: detail.into(),
        }
    }

    pub fn kind(&self) -> ErrorKind {
        match self {
            FmError::InvalidInput { .. } => ErrorKind::InvalidInput,
            FmError::NotFound { .. } => ErrorKind::NotFound,
            FmError::StateConflict { .. } => ErrorKind::StateConflict,
            FmError::ResourceExhausted { .. } => ErrorKind::ResourceExhausted,
            FmError::Corrupt { .. } => ErrorKind::Corrupt,
            FmError::ComputationFailed { .. } => ErrorKind::ComputationFailed,
        }
    }
}

impl From<io::Error> for FmError {
    /// 普通 I/O 错误归入计算失败；持久化层在“读出来对不上”的场合显式产生 [`FmError::Corrupt`]。
    fn from(e: io::Error) -> Self {
        match e.kind() {
            io::ErrorKind::NotFound => FmError::not_found(e.to_string()),
            _ => FmError::computation_failed(format!("io error: {e}")),
        }
    }
}

/// 模块内统一的 `Result` 别名。
pub type Result<T> = std::result::Result<T, FmError>;
