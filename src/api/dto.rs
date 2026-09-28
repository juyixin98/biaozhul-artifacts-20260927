//! HTTP 接口的数据传输对象（请求/响应 JSON 结构）。

use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReachabilityRequest {
    /// JSON 网对象（见 requests/*.json）。
    pub net: serde_json::Value,
    /// 可选：`.pnet` 文本格式；与 `net` 二选一。
    #[serde(default)]
    pub net_text: Option<String>,
    /// 库所名 -> 令牌数；缺省库所为 0。
    pub initial_marking: std::collections::HashMap<String, i64>,
    pub target_marking: std::collections::HashMap<String, i64>,
    #[serde(default)]
    pub state_limit: Option<u64>,
    #[serde(default)]
    pub invariant_coefficient_bound: Option<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InvariantsRequest {
    pub net: serde_json::Value,
    #[serde(default)]
    pub net_text: Option<String>,
    #[serde(default)]
    pub coefficient_bound: Option<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerifyFiringRequest {
    pub net: serde_json::Value,
    #[serde(default)]
    pub net_text: Option<String>,
    pub initial_marking: std::collections::HashMap<String, i64>,
    /// 待验证的变迁名序列。
    pub transition_sequence: Vec<String>,
    /// 可选：声称的终点标识；若提供则与独立重放结果强校验。
    #[serde(default)]
    pub claimed_final_marking: Option<std::collections::HashMap<String, i64>>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerifyInvariantRequest {
    pub net: serde_json::Value,
    #[serde(default)]
    pub net_text: Option<String>,
    #[serde(default)]
    pub initial_marking: Option<std::collections::HashMap<String, i64>>,
    #[serde(default)]
    pub target_marking: Option<std::collections::HashMap<String, i64>>,
    /// 待验证的候选权向量（按库所名）。
    pub weights: std::collections::HashMap<String, i64>,
}

#[derive(Debug, Serialize)]
pub struct NetSummary {
    pub place_count: usize,
    pub transition_count: usize,
    pub places: Vec<String>,
    pub transitions: Vec<String>,
    pub state_space_upper_bound: u128,
}

#[derive(Debug, Serialize)]
pub struct ReachabilityResponse {
    pub request_id: String,
    pub service: &'static str,
    pub version: &'static str,
    pub decision: String,
    pub reachable: bool,
    pub basis: String,
    pub certificate: Option<crate::kernel::ReachedCertificate>,
    pub invariant_obstruction: Option<crate::kernel::InvariantObstruction>,
    pub states_visited: u64,
    pub state_space_upper_bound: u128,
    pub state_limit: u64,
    pub progress: Vec<crate::kernel::SearchProgress>,
    pub scope: String,
    pub net: NetSummary,
}

#[derive(Debug, Serialize)]
pub struct InvariantsResponse {
    pub request_id: String,
    pub service: &'static str,
    pub version: &'static str,
    pub report: crate::kernel::PInvariantReport,
    pub net: NetSummary,
}

#[derive(Debug, Serialize)]
pub struct VerifyFiringResponse {
    pub request_id: String,
    pub service: &'static str,
    pub version: &'static str,
    pub valid: bool,
    pub replay: crate::verify::ReplayReport,
    pub claimed_final_matches: Option<bool>,
    /// 独立重放与内核 fire 的交叉一致性。
    pub kernel_agrees: bool,
}

#[derive(Debug, Serialize)]
pub struct VerifyInvariantResponse {
    pub request_id: String,
    pub service: &'static str,
    pub version: &'static str,
    pub report: crate::verify::InvariantCheckReport,
}

#[derive(Debug, Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
    pub service: &'static str,
    pub version: &'static str,
}

/// 统一错误包（不把异常/未知状态吞成成功）。
#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub error: String,
    pub message: String,
    pub request_id: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub issues: Vec<IssueDto>,
    /// 结构化失败细节（如被阻断的那一步：输入缺口/容量溢出）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<serde_json::Value>,
}

#[derive(Debug, Serialize)]
pub struct IssueDto {
    pub code: String,
    pub message: String,
    pub location: Option<String>,
}

pub const SERVICE_NAME: &str = "petri-reach";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
