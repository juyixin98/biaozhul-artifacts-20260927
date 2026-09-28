//! HTTP 请求/响应数据结构（serde DTO）。

use serde::{Deserialize, Serialize};

use crate::evidence::types::{Model, ResolutionProof};

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SolveRequest {
    /// 调用方自带请求标识；缺省由服务端生成。
    pub request_id: Option<String>,
    /// DIMACS CNF 正文（与 clauses 二选一）。
    pub dimacs: Option<String>,
    /// 结构化子句：每条为带符号整数数组，0 不是文字。
    pub clauses: Option<Vec<Vec<i64>>>,
    /// 结构化输入的变量数。
    pub num_vars: Option<usize>,
    /// 请求级预算（只能比服务端配置更紧）。
    pub time_limit_ms: Option<u64>,
    pub max_propagations: Option<u64>,
    pub max_decisions: Option<u64>,
}

/// 验证端点共用的公式输入。
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerifyRequest {
    pub dimacs: Option<String>,
    pub clauses: Option<Vec<Vec<i64>>>,
    pub num_vars: Option<usize>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerifyModelRequest {
    pub request_id: Option<String>,
    #[serde(flatten)]
    pub formula: VerifyRequest,
    pub model: Model,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerifyProofRequest {
    pub request_id: Option<String>,
    #[serde(flatten)]
    pub formula: VerifyRequest,
    pub proof: ResolutionProof,
}

/// 便于文档与示例的响应骨架类型。
#[derive(Debug, Clone, Serialize)]
pub struct SolveResponseShape {
    pub request_id: String,
    pub accepted: bool,
    pub outcome_tag: &'static str,
}
