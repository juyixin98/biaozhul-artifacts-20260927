//! 可检验证据的数据契约：SAT 模型与 UNSAT 归结（resolution）推导记录。
//!
//! 两类证据都可由**独立**检查器复核：
//! - SAT：模型必须逐条满足规范化后的原始子句；
//! - UNSAT：一条线性归结链，每一步以一个父子句（输入或已推导出的引理）为
//!   "主句"，与若干"边句"依次归结，最终推导出空子句。
//!
//! 证明只引用**规范化后的原始子句编号**（`input:<idx>`）或此前已确立的
//! 引理编号（`lemma:<id>`）。引理 id 必须严格递增、先定义后引用。

use serde::{Deserialize, Serialize};

use crate::lit::Lit;

/// 一个父子句的引用。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum ClauseRef {
    /// 引用规范化后原始公式的第 idx 条子句（从 0 开始）。
    Input { idx: usize },
    /// 引用此前推导步确立的引理。
    Lemma { id: u64 },
}

/// 单步归结：`main` 依次与 `side[*]` 归结，得到 `resolvent`。
///
/// 合法条件：每次归结必须在恰好一个互补对上进行；中间结果允许重言
/// （检查器不禁止），但最终 `resolvent` 必须与逐次归结的集合相等。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProofStep {
    /// 引理编号，全局递增、从 0 开始；必须按数组顺序出现。
    pub id: u64,
    /// 主句（被归结的核心子句），其文字必须是输入子句或先前列引理的完整文字。
    pub main: ClauseRef,
    /// 边句（原因子句），按归结发生顺序排列。
    pub side: Vec<ClauseRef>,
    /// 归结消元所用的枢轴变量（编码文字的变量部分；正负无所谓，检查器
    /// 同时尝试两种极性）。记录它是为了让拒绝诊断更具体。
    pub pivot_vars: Vec<u32>,
    /// 推导结果文字（编码后的 Lit，升序、无重复）。
    pub resolvent: Vec<Lit>,
}

/// 完整 UNSAT 证据。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResolutionProof {
    /// 推导步序列；最后一步的 resolvent 必须为空子句。
    pub steps: Vec<ProofStep>,
}

/// SAT 模型：以编码文字给出的完整赋值（每个有效变量恰好一个极性）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Model {
    /// 为真的编码文字集合；对变量 1..=num_vars 全覆盖。
    pub true_literals: Vec<Lit>,
    pub num_vars: usize,
}

/// 求解结论。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "result", rename_all = "UPPERCASE")]
pub enum Outcome {
    Sat {
        model: Model,
    },
    Unsat {
        proof: ResolutionProof,
    },
    /// 预算（时间/搜索步数）耗尽：既不是 SAT 也不是 UNSAT 的可靠结论。
    Unknown {
        reason: String,
        /// 耗尽时已完成的部分证明（可能为空），不声称证明任何结论。
        partial_proof: ResolutionProof,
    },
}

/// 规范化审计信息（与结果一起返回，便于复现与排障）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NormalizeReport {
    pub num_vars_effective: usize,
    pub num_clauses_after: usize,
    pub duplicate_literals_removed: usize,
    pub tautology_clauses_removed: usize,
    pub has_empty_clause: bool,
}
