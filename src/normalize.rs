//! 子句规范化：重复文字、互补文字（重言式）与空子句在求解前统一处理。
//!
//! 规范化是纯函数式的，输入子句按首次出现顺序保留，子句内文字升序排列。
//! 求解器与独立验证器各自实现同样的规范化逻辑（见 `evidence::check` 与
//! `tests/common/oracle.rs`），互不共享代码，以免"同源错误"互相背书。

use std::collections::HashSet;

use crate::lit::{lit_from_signed, Lit, Var};

/// 规范化结果与审计信息。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NormalizedCnf {
    /// 声明的变量数（DIMACS 头 `p cnf n m` 的 n；结构化输入为显式给出的 num_vars）。
    pub declared_vars: usize,
    /// 规范化后子句；空子句以空 `Vec` 表示，一旦出现公式立即不可满足。
    pub clauses: Vec<Vec<Lit>>,
    /// 实际出现过的最大变量号（无文字时为 0）。
    pub max_var_seen: Var,
    /// 被去掉的重复文字出现总次数。
    pub duplicate_literals_removed: usize,
    /// 被整体丢弃的重言子句数（含互补对）。
    pub tautology_clauses_removed: usize,
}

impl NormalizedCnf {
    /// 是否含空子句（直接 UNSAT 的充分条件）。
    pub fn has_empty_clause(&self) -> bool {
        self.clauses.iter().any(|c| c.is_empty())
    }

    /// 有效变量数：声明值与实际最大值的较大者。
    pub fn effective_vars(&self) -> usize {
        self.declared_vars.max(self.max_var_seen as usize)
    }
}

/// 规范化一组"带符号整数"形式的子句。
///
/// - 空输入子句原样保留为空子句（触发直接 UNSAT）；
/// - 同一子句内重复文字只保留一次；
/// - 同一子句内含互补对则整条删除（重言式对合取式无约束）。
pub fn normalize_signed_clauses(
    declared_vars: usize,
    raw_clauses: &[Vec<i64>],
) -> NormalizedCnf {
    let mut clauses = Vec::with_capacity(raw_clauses.len());
    let mut dup_removed = 0usize;
    let mut taut_removed = 0usize;
    let mut max_var: Var = 0;

    for raw in raw_clauses {
        if raw.is_empty() {
            clauses.push(Vec::new());
            continue;
        }
        let mut seen: HashSet<Lit> = HashSet::new();
        let mut lits: Vec<Lit> = Vec::new();
        let mut tautology = false;
        for &s in raw {
            let lit = lit_from_signed(s);
            max_var = max_var.max(crate::lit::var_of(lit));
            if seen.contains(&lit) {
                dup_removed += 1;
                continue;
            }
            if seen.contains(&crate::lit::neg(lit)) {
                tautology = true;
            }
            seen.insert(lit);
            lits.push(lit);
        }
        if tautology {
            taut_removed += 1;
            continue;
        }
        lits.sort_unstable();
        clauses.push(lits);
    }

    NormalizedCnf {
        declared_vars,
        clauses,
        max_var_seen: max_var,
        duplicate_literals_removed: dup_removed,
        tautology_clauses_removed: taut_removed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dedups_reorders_and_keeps_empty() {
        let n = normalize_signed_clauses(
            3,
            &[vec![3, 1, 3, -2], vec![], vec![2, -2, 1]],
        );
        // 第 1 条：3 重复一次 → 排序为 [x1=2, ¬x2=5, x3=6]
        assert_eq!(n.clauses[0], vec![2, 5, 6]);
        assert_eq!(n.duplicate_literals_removed, 1);
        assert!(n.clauses[1].is_empty());
        assert_eq!(n.clauses.len(), 2);
        assert_eq!(n.tautology_clauses_removed, 1);
        assert!(n.has_empty_clause());
    }

    #[test]
    fn effective_vars_takes_max() {
        let n = normalize_signed_clauses(5, &[vec![1, 2]]);
        assert_eq!(n.effective_vars(), 5);
        let n2 = normalize_signed_clauses(1, &[vec![1, 4]]);
        assert_eq!(n2.effective_vars(), 4);
    }
}
