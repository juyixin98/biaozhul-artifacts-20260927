//! 证据类型：SAT 模型与 UNSAT 线性消解推导记录的序列化结构。
//!
//! 这些类型是求解内核（[`crate::solver`]）与独立检查器（[`crate::verify`]）
//! 之间的“契约”：检查器只依赖本模块与 [`crate::cnf`]，不导入求解器代码。

use serde::{Deserialize, Serialize};

use crate::cnf::Lit;

/// 一次消解步骤：把当前派生式与 `with_ref` 引用的子句在变量 `pivot_var` 上消解。
///
/// 约定：当前派生式含该变量负极性文字，被引用子句含正极性文字（反之亦然）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResolventOp {
    pub pivot_var: u32,
    /// 引用：`"i<n>"` = 规范化后的第 n 条输入子句；`"d<n>"` = 同份证明中先前的派生条目。
    pub with_ref: String,
}

/// 一条派生子句：从 `start_ref` 子句出发，依次执行 `resolvents` 中的消解，
/// 所得结果必须与 `literals` 完全一致。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DerivedClause {
    pub id: String,
    pub literals: Vec<Lit>,
    pub start_ref: String,
    pub resolvents: Vec<ResolventOp>,
}

/// UNSAT 推导记录：`derived_clauses` 必须顺序可验证，
/// 且 `empty_clause_ref` 指向的（输入或派生）子句是空子句。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResolutionProof {
    pub derived_clauses: Vec<DerivedClause>,
    pub empty_clause_ref: String,
}
