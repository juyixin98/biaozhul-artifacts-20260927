//! CNF 数据模型与子句规范化。
//!
//! 职责边界：
//! - [`Lit`] 是 DIMACS 风格的有符号整数（`v` 表示正文字，`-v` 表示负文字，禁止 0）。
//! - [`Clause`] 规范化后保证：去重、无互补文字、确定性排序；空子句保留（它是 UNSAT 证据）。
//! - [`Formula`] 只持有规范化结果；原始输入经过 [`normalize_formula`] 才能进入求解内核。

use serde::{Deserialize, Serialize};

/// 有符号文字，非零：正文字 `v`，负文字 `-v`。
pub type Lit = i32;

/// 变量编号（从 1 开始）。
pub type Var = usize;

/// 规范化后的子句。空 `lits` 表示空子句（恒假）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Clause {
    pub lits: Vec<Lit>,
}

/// 规范化后的 CNF 公式。
#[derive(Debug, Clone)]
pub struct Formula {
    pub num_vars: usize,
    pub clauses: Vec<Clause>,
}

/// 规范化期间发生的、值得在诊断中展示的事件。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum NormNote {
    /// 子句中删除了重复文字。
    DuplicateLitsRemoved {
        clause_index: usize,
        original_len: usize,
        normalized_len: usize,
    },
    /// 子句含互补对，是重言式，整条丢弃。
    TautologyDropped { clause_index: usize },
    /// 发现空子句（公式必然不可满足），保留以便求解器直接给出冲突证据。
    EmptyClause { clause_index: usize },
}

/// 规范化失败类别（与求解逻辑无关的输入错误）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NormError {
    ZeroLiteral,
    VarOutOfRange {
        clause_index: usize,
        lit: Lit,
        num_vars: usize,
    },
}

impl std::fmt::Display for NormError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            NormError::ZeroLiteral => write!(f, "literal 0 is only allowed as a clause terminator"),
            NormError::VarOutOfRange {
                clause_index,
                lit,
                num_vars,
            } => write!(
                f,
                "literal {lit} in clause {clause_index} references variable outside 1..={num_vars}"
            ),
        }
    }
}

impl std::error::Error for NormError {}

/// 单子句规范化：
///
/// - 排序后去重；
/// - 同时出现 `x` 与 `-x` 时判定为重言式，返回 `None`（整条丢弃）；
/// - 空输入返回空 `Vec`，即空子句（不丢弃）。
///
/// 排序按 (变量编号, 极性)，保证规范化结果对输入顺序不敏感。
pub fn normalize_clause(lits: &[Lit]) -> Option<Vec<Lit>> {
    let mut sorted: Vec<Lit> = lits.to_vec();
    sorted.sort_by_key(|l| (l.unsigned_abs(), *l < 0));
    sorted.dedup();
    // 排序后互补对必然相邻，顺序扫描即可。
    let mut i = 0;
    while i + 1 < sorted.len() {
        if sorted[i] == -sorted[i + 1] {
            return None; // 互补文字 ⇒ 重言子句
        }
        i += 1;
    }
    Some(sorted)
}

/// 构造规范化公式，返回公式与规范化诊断。
///
/// `num_vars == None` 时从文字推断最大变量数。
pub fn normalize_formula(
    declared_vars: Option<usize>,
    raw: &[Vec<Lit>],
) -> Result<(Formula, Vec<NormNote>), NormError> {
    let mut num_vars = declared_vars.unwrap_or(0);
    for (ci, clause) in raw.iter().enumerate() {
        for &lit in clause {
            if lit == 0 {
                return Err(NormError::ZeroLiteral);
            }
            let v = lit.unsigned_abs() as usize;
            if let Some(n) = declared_vars {
                if v > n {
                    return Err(NormError::VarOutOfRange {
                        clause_index: ci,
                        lit,
                        num_vars: n,
                    });
                }
            }
            num_vars = num_vars.max(v);
        }
    }

    let mut clauses = Vec::with_capacity(raw.len());
    let mut notes = Vec::new();
    for (ci, raw_clause) in raw.iter().enumerate() {
        match normalize_clause(raw_clause) {
            None => notes.push(NormNote::TautologyDropped { clause_index: ci }),
            Some(lits) => {
                if lits.len() != raw_clause.len() {
                    notes.push(NormNote::DuplicateLitsRemoved {
                        clause_index: ci,
                        original_len: raw_clause.len(),
                        normalized_len: lits.len(),
                    });
                }
                if lits.is_empty() {
                    notes.push(NormNote::EmptyClause { clause_index: ci });
                }
                clauses.push(Clause { lits });
            }
        }
    }
    Ok((Formula { num_vars, clauses }, notes))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn removes_duplicate_literals() {
        assert_eq!(normalize_clause(&[1, 1, -2, -2, 3]), Some(vec![1, -2, 3]));
    }

    #[test]
    fn drops_tautology() {
        assert_eq!(normalize_clause(&[2, -1, 1, 3]), None);
    }

    #[test]
    fn keeps_empty_clause() {
        assert_eq!(normalize_clause(&[]), Some(vec![]));
    }

    #[test]
    fn normalization_is_order_insensitive() {
        let a = normalize_clause(&[-2, 3, 1]).unwrap();
        let b = normalize_clause(&[3, 1, -2]).unwrap();
        assert_eq!(a, b);
    }

    #[test]
    fn rejects_zero_and_out_of_range() {
        assert_eq!(
            normalize_formula(Some(2), &[vec![3]]).unwrap_err(),
            NormError::VarOutOfRange {
                clause_index: 0,
                lit: 3,
                num_vars: 2
            }
        );
        assert_eq!(
            normalize_formula(Some(2), &[vec![1, 0]]).unwrap_err(),
            NormError::ZeroLiteral
        );
    }

    #[test]
    fn infers_num_vars() {
        let (f, _) = normalize_formula(None, &[vec![1, -3]]).unwrap();
        assert_eq!(f.num_vars, 3);
    }
}
