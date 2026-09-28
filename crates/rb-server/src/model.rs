//! 统一响应/请求模型（可解释信封）。

use serde::{Deserialize, Serialize};

/// 所有处理器返回的统一信封。
#[derive(Debug, Clone, Serialize)]
pub struct Envelope<T: Serialize> {
    /// 关联请求身份（与 `x-request-id` 头一致）。
    pub request_id: String,
    /// 数据格式语义版本（十六进制）。
    pub format_version: String,
    /// 成功时的结果；失败为 null。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<T>,
    /// 失败原因（与成功互斥；即使成功也可能携带非致命警告）。
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub errors: Vec<ErrorDetail>,
    /// 不确定结论 / 需人工关注事项，单列展示，绝不与成功结果混写。
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub notes: Vec<String>,
    /// 关键处理步骤（可解释性：每步附简短描述与位置）。
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub steps: Vec<Step>,
}

impl<T: Serialize> Envelope<T> {
    pub fn ok(request_id: impl Into<String>, result: T) -> Self {
        Envelope {
            request_id: request_id.into(),
            format_version: format!("{:#010x}", rb_format::FORMAT_VERSION),
            result: Some(result),
            errors: Vec::new(),
            notes: Vec::new(),
            steps: Vec::new(),
        }
    }

    pub fn with_step(mut self, stage: impl Into<String>, detail: impl Into<String>) -> Self {
        self.steps.push(Step {
            stage: stage.into(),
            detail: detail.into(),
        });
        self
    }

    pub fn with_note(mut self, note: impl Into<String>) -> Self {
        self.notes.push(note.into());
        self
    }
}

/// 一条处理步骤。
#[derive(Debug, Clone, Serialize)]
pub struct Step {
    /// 阶段，例如 `load`、`op:intersect`、`rank`。
    pub stage: String,
    /// 人类可读细节（分片键、偏移、算法路径等）。
    pub detail: String,
}

/// 结构化失败原因。
#[derive(Debug, Clone, Serialize)]
pub struct ErrorDetail {
    /// 稳定的错误代码（与内部错误类别对应，便于断言）。
    pub code: String,
    pub message: String,
    /// 可选的定位信息（如容器键、字节偏移）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub location: Option<String>,
}

// ---------------- 请求体 ----------------

#[derive(Debug, Clone, Deserialize)]
pub struct CreateReq {
    /// 初始成员（可缺省）。
    #[serde(default)]
    pub values: Vec<u32>,
    /// 为 true 时名称已存在则报错（默认 false = 覆盖）。
    #[serde(default)]
    pub expect_new: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ValuesReq {
    pub values: Vec<u32>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct OpReq {
    /// 另一个参与运算的已持久化集合名。
    pub with: String,
}

// ---------------- 结果体 ----------------

#[derive(Debug, Clone, Serialize)]
pub struct SetSummary {
    pub name: String,
    pub cardinality: u64,
    pub containers: usize,
    pub container_kinds: ContainerKinds,
    pub min: Option<u32>,
    pub max: Option<u32>,
    /// 是否将结果持久化（运算默认不落盘）。
    pub persisted: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct ContainerKinds {
    pub array: usize,
    pub bitmap: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct ContainsResult {
    pub value: u32,
    pub contains: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct RankResult {
    pub x: u32,
    /// 严格小于 x 的元素个数。
    pub rank: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct SelectResult {
    pub i: u64,
    pub value: Option<u32>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ValuesResult {
    pub values: Vec<u32>,
    pub truncated: bool,
    pub limit: usize,
}
