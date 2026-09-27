//! 核心层错误类型。不把任何异常/未知状态折叠成成功。

use std::fmt;

/// 失败类别（稳定字符串，API 层与测试直接断言）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailureKind {
    /// 参数不合法。
    InvalidParams,
    /// 过滤器已满：两个候选桶都无空槽且迁移次数达到上限（插入已回滚）。
    FilterFull,
    /// 指纹在两个候选桶中均不存在。
    NotPresent,
    /// 凭证签名不合法 / 与键不匹配 / 已被消费。
    InvalidCredential,
    /// 凭证存在但对应插入计数已经用完（重复删除同一凭证）。
    CredentialExhausted,
}

impl FailureKind {
    pub fn as_str(self) -> &'static str {
        match self {
            FailureKind::InvalidParams => "INVALID_PARAMS",
            FailureKind::FilterFull => "FILTER_FULL",
            FailureKind::NotPresent => "NOT_PRESENT",
            FailureKind::InvalidCredential => "INVALID_CREDENTIAL",
            FailureKind::CredentialExhausted => "CREDENTIAL_EXHAUSTED",
        }
    }
}

impl fmt::Display for FailureKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, thiserror::Error)]
pub enum CoreError {
    #[error("{0}")]
    InvalidParams(String),
    #[error("过滤器已满：迁移 {} 次仍无空槽，插入已回滚", .0)]
    FilterFull(u32),
    #[error("候选桶中不存在该指纹")]
    NotPresent,
    #[error("凭证无效：{0}")]
    InvalidCredential(String),
    #[error("凭证已被消费，不能重复删除")]
    CredentialExhausted,
}

impl CoreError {
    pub fn kind(&self) -> FailureKind {
        match self {
            CoreError::InvalidParams(_) => FailureKind::InvalidParams,
            CoreError::FilterFull(_) => FailureKind::FilterFull,
            CoreError::NotPresent => FailureKind::NotPresent,
            CoreError::InvalidCredential(_) => FailureKind::InvalidCredential,
            CoreError::CredentialExhausted => FailureKind::CredentialExhausted,
        }
    }
}

pub type CoreResult<T> = Result<T, CoreError>;
