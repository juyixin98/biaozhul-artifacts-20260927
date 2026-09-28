//! 求解器内部的子句存储。
//!
//! 与证据层的区别：`Clause` 带有可变的双监视文字位置与"来源"，仅在
//! 求解器内部使用；证据（证明步骤）单独保存在 `ResolutionProof` 中。

use crate::evidence::types::ClauseRef;
use crate::lit::Lit;

/// 子句来源：原始输入（携带规范化后的子句下标）或某一步学到的引理。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClauseOrigin {
    /// 规范化后输入公式的第 idx 条子句。
    Input(usize),
    /// 由第 id 步归结推导而来。
    Learned(u64),
}

impl ClauseOrigin {
    pub fn as_clause_ref(&self) -> ClauseRef {
        match self {
            ClauseOrigin::Input(idx) => ClauseRef::Input { idx: *idx },
            ClauseOrigin::Learned(id) => ClauseRef::Lemma { id: *id },
        }
    }
}

/// 一条子句。`lits[0]` 与 `lits[1]` 是两个被监视的文字。
#[derive(Debug, Clone)]
pub struct Clause {
    pub lits: Vec<Lit>,
    pub origin: ClauseOrigin,
}

impl Clause {
    pub fn new(lits: Vec<Lit>, origin: ClauseOrigin) -> Self {
        debug_assert!(lits.len() >= 2, "单位/空子句不入双监视表");
        Clause { lits, origin }
    }
}

/// 子句在求解器子句数据库中的下标。
pub type ClauseId = usize;
