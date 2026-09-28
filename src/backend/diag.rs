//! 请求标识与诊断。
//!
//! - 每个请求带 [`RequestId`]：优先取入站 `x-request-id`，否则生成
//!   `req-<16 个十六进制字符>`；处理器产出的接受/拒绝/无法判定结论与错误体
//!   都回带同一标识并写入日志。
//! - 脱敏：敏感表达式只输出 FNV-1a 指纹（`expr_sha=...`），日志与错误体中
//!   不出现原文。FNV-1a 是非加密指纹，仅用于“日志可关联、内容不外泄”。

use std::sync::atomic::{AtomicU64, Ordering};

use serde::Serialize;

/// 请求标识。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RequestId(pub String);

impl RequestId {
    /// 规范化入站请求 ID：非空且只含安全字符时沿用，否则生成新的。
    pub fn from_header(raw: Option<&str>) -> Self {
        if let Some(v) = raw {
            let v = v.trim();
            if !v.is_empty()
                && v.len() <= 128
                && v.chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
            {
                return RequestId(v.to_string());
            }
        }
        RequestId::generate()
    }

    /// 进程内生成：时间无关、可重复测试的单调序号 + 进程随机基数。
    pub fn generate() -> Self {
        static SEQ: AtomicU64 = AtomicU64::new(1);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let mix = fnv1a(format!("robdd-pid-{:?}-{seq}", std::process::id()).as_bytes());
        RequestId(format!("req-{mix:016x}"))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// FNV-1a 64 位指纹（脱敏指纹，不做安全用途）。
pub fn fnv1a(data: &[u8]) -> u64 {
    const OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut h = OFFSET;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(PRIME);
    }
    h
}

/// 处理器结论类别。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Decision {
    /// 接受：请求合法且已完成运算/判定。
    Accepted,
    /// 拒绝：请求本身不合法（语法、越权使用他管理器边、映射非双射等）。
    Rejected,
    /// 无法判定：请求合法，但证据侧无法给出确定结论（变量过多等）。
    Inconclusive,
}

/// 进入日志/响应体的关键状态快照。键值全部为脱敏后内容。
pub type DiagState = serde_json::Map<String, serde_json::Value>;

/// 统一诊断记录。
#[derive(Debug, Clone, Serialize)]
pub struct Diagnostic {
    pub request_id: String,
    pub decision: Decision,
    /// 机器可读原因码，例如 `unknown_variable`。
    pub reason: String,
    /// 关键状态（管理器数、节点数、映射规模、见证数等，不含表达式原文）。
    pub state: DiagState,
    /// 脱敏后的表达式指纹（仅在请求涉及表达式时出现）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expr_fp: Option<String>,
}

impl Diagnostic {
    pub fn new(rid: &RequestId, decision: Decision, reason: impl Into<String>) -> Self {
        Self {
            request_id: rid.0.clone(),
            decision,
            reason: reason.into(),
            state: DiagState::new(),
            expr_fp: None,
        }
    }

    pub fn with_state(mut self, state: DiagState) -> Self {
        self.state = state;
        self
    }

    pub fn with_expr_fp(mut self, fp: Option<String>) -> Self {
        self.expr_fp = fp;
        self
    }

    /// 输出结构化日志行。
    pub fn log(&self) {
        match self.decision {
            Decision::Accepted => {
                tracing::info!(target: "robdd.diag", diag = %serde_json::to_string(self).unwrap_or_default(), "accepted")
            }
            Decision::Rejected => {
                tracing::warn!(target: "robdd.diag", diag = %serde_json::to_string(self).unwrap_or_default(), "rejected")
            }
            Decision::Inconclusive => {
                tracing::info!(target: "robdd.diag", diag = %serde_json::to_string(self).unwrap_or_default(), "inconclusive")
            }
        }
    }
}

/// 请求级上下文：标识 + 是否脱敏。处理器通过它构造诊断与错误。
#[derive(Debug, Clone)]
pub struct Ctx {
    pub rid: RequestId,
    pub sensitive: bool,
}

impl Ctx {
    pub fn new(rid: RequestId, sensitive: bool) -> Self {
        Self { rid, sensitive }
    }

    /// 对表达式原文做脱敏处理：敏感时返回指纹，否则返回截断原文（上限 200 字符）。
    pub fn redact_expr(&self, raw: &str) -> Option<String> {
        if self.sensitive {
            Some(format!("fnv1a:{:016x}", fnv1a(raw.as_bytes())))
        } else {
            if raw.len() <= 200 {
                Some(raw.to_string())
            } else {
                Some(format!("{}...(truncated)", &raw[..200]))
            }
        }
    }

    pub fn diag(&self, decision: Decision, reason: impl Into<String>) -> Diagnostic {
        Diagnostic::new(&self.rid, decision, reason)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_id_header_validation() {
        assert_eq!(
            RequestId::from_header(Some("  abc-123_X. ")).as_str(),
            "abc-123_X."
        );
        assert!(RequestId::from_header(Some("bad id!"))
            .as_str()
            .starts_with("req-"));
        assert!(RequestId::from_header(Some(""))
            .as_str()
            .starts_with("req-"));
        assert!(RequestId::from_header(None).as_str().starts_with("req-"));
    }

    #[test]
    fn fnv1a_known_vector_and_redaction_hides_content() {
        // FNV-1a("") = offset basis。
        assert_eq!(fnv1a(b""), 0xcbf2_9ce4_8422_2325);
        let ctx = Ctx::new(RequestId::generate(), true);
        let fp = ctx.redact_expr("secret_var & x").unwrap();
        assert!(fp.starts_with("fnv1a:"));
        assert!(!fp.contains("secret_var"));
        let ctx2 = Ctx::new(RequestId::generate(), false);
        assert_eq!(ctx2.redact_expr("a & b").unwrap(), "a & b");
    }
}
