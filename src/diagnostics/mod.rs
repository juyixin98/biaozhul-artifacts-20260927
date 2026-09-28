//! 诊断：请求标识、关键求解状态、敏感信息脱敏。
//!
//! 设计原则：
//! - 每条求解记录都带 `request_id`（调用方提供或服务端生成），贯穿日志与响应；
//! - 日志只记录**结构与计数**（变量数、子句数、决策/冲突数、结论、预算），
//!   默认不回显公式正文——公式可能是调用方的敏感数据；
//! - 若确需打印公式片段（排障），统一走 [`redact_dimacs`] 脱敏。

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::solver::Counters;

static REQ_SEQ: AtomicU64 = AtomicU64::new(0);

/// 生成确定性足够、无需外部依赖的请求 id：`req-<nanos>-<seq>`。
pub fn new_request_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let seq = REQ_SEQ.fetch_add(1, Ordering::Relaxed);
    format!("req-{nanos:x}-{seq:04x}")
}

/// 一次求解的关键状态快照（用于日志与响应 diagnostics 字段）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SolveDiagnostics {
    pub request_id: String,
    pub outcome: &'static str,
    pub num_vars: usize,
    pub num_clauses: usize,
    pub decisions: u64,
    pub propagations: u64,
    pub conflicts: u64,
    pub learned_clauses: u64,
    /// 接受/拒绝/无法判定的一句话理由。
    pub verdict_reason: String,
    /// 预算是否耗尽（仅 UNKNOWN 时为 true）。
    pub budget_exhausted: bool,
}

impl SolveDiagnostics {
    pub fn new(
        request_id: impl Into<String>,
        outcome: &'static str,
        num_vars: usize,
        num_clauses: usize,
        counters: &Counters,
        verdict_reason: impl Into<String>,
        budget_exhausted: bool,
    ) -> Self {
        SolveDiagnostics {
            request_id: request_id.into(),
            outcome,
            num_vars,
            num_clauses,
            decisions: counters.decisions,
            propagations: counters.propagations,
            conflicts: counters.conflicts,
            learned_clauses: counters.learned_clauses,
            verdict_reason: verdict_reason.into(),
            budget_exhausted,
        }
    }
}

/// DIMACS 正文脱敏：保留头部形状，文字替换为 `#`，每行截断到 64 字符。
///
/// 用于错误日志中回显输入片段，避免把真实变量布局当作明文写出。
pub fn redact_dimacs(input: &str) -> String {
    let mut out = String::new();
    for line in input.lines().take(8) {
        let trimmed = line.trim();
        if trimmed.starts_with('c') {
            out.push_str("c <redacted comment>");
        } else if trimmed.starts_with('p') {
            out.push_str(line);
        } else {
            let redacted: String = line
                .split_whitespace()
                .map(|tok| {
                    if tok.parse::<i64>().is_ok() {
                        "#"
                    } else {
                        "<redacted>"
                    }
                })
                .collect::<Vec<_>>()
                .join(" ");
            out.push_str(&redacted);
        }
        out.push('\n');
    }
    if out.len() > 512 {
        out.truncate(512);
        out.push('…');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_ids_are_unique() {
        let a = new_request_id();
        let b = new_request_id();
        assert_ne!(a, b);
        assert!(a.starts_with("req-"));
    }

    #[test]
    fn redaction_hides_literals_and_keeps_header() {
        let src = "p cnf 100 2\n1 -2 3 0\nc secret customer info\n4 5 0\n";
        let r = redact_dimacs(src);
        assert!(r.contains("p cnf 100 2"), "头部应保留: {r}");
        assert!(!r.contains("secret"), "注释必须脱敏: {r}");
        assert!(!r.contains("-2"), "文字必须脱敏: {r}");
        assert!(r.contains('#'));
    }
}
