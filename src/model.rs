//! 输入语言：约束集合服务的请求/响应数据契约与输入校验。
//!
//! 约束的文本形式为 **`x - y <= c`**（`x`、`y` 为变量名，`c` 为 64 位有界整数）。
//! 所有对外字段均使用 snake_case；数值一律为 `i64`，不做静默取整。

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

/// 一条用户命名约束：`x - y <= c`。
///
/// `name` 是该约束在某个集合内的持久 ID；冲突证据只引用该名字，
/// 永不暴露内核使用的匿名边（超源边）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConstraintInput {
    /// 集合内唯一的约束名（1..=64 字符的标识符）。
    pub name: String,
    /// 差的左端变量。
    pub x: String,
    /// 差的右端变量。
    pub y: String,
    /// 常数上界。
    pub c: i64,
}

/// `POST /v1/constraints:batch`：原子追加一批命名约束。
#[derive(Debug, Clone, Deserialize)]
pub struct BatchRequest {
    pub constraints: Vec<ConstraintInput>,
}

/// `POST /v1/reset`：用给定集合原子替换当前集合（省略 constraints 表示清空）。
#[derive(Debug, Clone, Default, Deserialize)]
pub struct ResetRequest {
    #[serde(default)]
    pub constraints: Vec<ConstraintInput>,
}

/// 原子批次提交成功后的响应。
#[derive(Debug, Clone, Serialize)]
pub struct BatchResponse {
    /// 提交后的集合版本（每次成功原子变更 +1）。
    pub version: u64,
    /// 提交后的约束总数。
    pub constraint_count: usize,
    /// 提交后的变量总数。
    pub variable_count: usize,
}

/// `DELETE /v1/constraints/{name}` 的响应。
#[derive(Debug, Clone, Serialize)]
pub struct RemoveResponse {
    pub version: u64,
    pub removed: String,
    pub constraint_count: usize,
}

/// 约束集合快照（`GET /v1/constraints`）。
#[derive(Debug, Clone, Serialize)]
pub struct ConstraintsView {
    pub version: u64,
    pub constraints: Vec<ConstraintInput>,
}

/// 可行解（`GET /v1/solution`、`POST /v1/solve`）。
#[derive(Debug, Clone, Serialize)]
pub struct SolutionResponse {
    /// 集合版本（无状态 solve 时省略）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub version: Option<u64>,
    /// 变量 -> 整数赋值。每个变量都出现；未使用变量不会凭空出现。
    pub assignment: BTreeMap<String, i64>,
}

/// 负环上的一步，回溯到一条原约束。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EvidenceEdgeDto {
    /// 该边对应的原约束名（不会是匿名内部边）。
    pub constraint: String,
    /// 边的源变量（约束 `x - y <= c` 对应图边 `y -> x`，此处给出源 y）。
    pub from: String,
    /// 边的目标变量。
    pub to: String,
    pub weight: i64,
}

/// 不可满足时的冲突证据：一个完全由命名约束构成的严格负环。
#[derive(Debug, Clone, Serialize)]
pub struct ConflictDto {
    /// 环上的边，按行走顺序排列（`edges[i].to == edges[i+1].from`，首尾闭合）。
    pub cycle: Vec<EvidenceEdgeDto>,
    /// 环费用。验证保证其严格为负，且使用 checked 加法得到。
    pub total_cost: i64,
}

/// `POST /v1/solve`：对一次性约束集合求解，不修改服务端状态。
#[derive(Debug, Clone, Deserialize)]
pub struct SolveRequest {
    pub constraints: Vec<ConstraintInput>,
}

/// `POST /v1/evidence/verify`：提交一个声称的冲突环进行独立复核。
///
/// 可带一次性 `constraints` 做纯输入验证；省略时针对服务端当前集合复核。
#[derive(Debug, Clone, Default, Deserialize)]
pub struct VerifyRequest {
    /// 声称的环，给出环上各约束的名字，按行走顺序排列。
    pub cycle: Vec<String>,
    #[serde(default)]
    pub constraints: Option<Vec<ConstraintInput>>,
}

/// 证据验证结论（HTTP 恒为 200；只有输入/状态/计算类问题才给非 2xx）。
#[derive(Debug, Clone, Serialize)]
pub struct VerifyResponse {
    pub valid: bool,
    /// valid=false 时给出判定理由（结构或语义层面）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    /// valid=true 时回显复核出的环与严格负费用。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub evidence: Option<ConflictDto>,
}

/// 校验一条输入约束。返回规范化结果或人类可读原因（调用方归入 input_error）。
pub fn validate_constraint(input: &ConstraintInput) -> Result<(), String> {
    validate_ident(&input.name, "constraint name")?;
    validate_ident(&input.x, "variable x")?;
    validate_ident(&input.y, "variable y")?;
    Ok(())
}

/// 标识符规则：1..=64 字符，字母/下划线开头，其余为字母、数字、下划线。
pub fn validate_ident(s: &str, what: &str) -> Result<(), String> {
    let mut chars = s.chars();
    let first = chars
        .next()
        .ok_or_else(|| format!("{what} must not be empty"))?;
    if !first.is_ascii_alphabetic() && first != '_' {
        return Err(format!(
            "{what} must start with a letter or underscore: {s:?}"
        ));
    }
    if !chars.all(|ch| ch.is_ascii_alphanumeric() || ch == '_') {
        return Err(format!(
            "{what} contains characters outside [A-Za-z0-9_]: {s:?}"
        ));
    }
    if s.len() > 64 {
        return Err(format!(
            "{what} must be at most 64 bytes, got {}: {s:?}",
            s.len()
        ));
    }
    Ok(())
}
