//! 测试专用的独立 oracle：对 1..=n 的全部 2^n 个赋值做暴力真值表枚举。
//!
//! 这份代码刻意不引用 `cnf_dpll` 的求解器、规范器或检查器，只自己实现
//! DIMACS 整数到真值的朴素判定。它给出的结论是验收测试里的"参考答案"，
//! 而参考答案不是由被测核心产生的。
//!
//! 仅适用于少量变量（测试中 n ≤ 20）。

/// 暴力枚举结论。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BruteResult {
    /// 可满足：返回字典序最小（变量号小者优先取真）的全赋值。
    /// assignment[v-1] 为变量 v 的真值。
    Sat { assignment: Vec<bool> },
    Unsat,
}

/// `clauses` 为带符号整数（无 0）；空切片表示空子句 → 直接 UNSAT。
/// 变量范围按 num_vars 与出现过的最大绝对值的较大者计。
pub fn brute_force(num_vars: usize, clauses: &[Vec<i64>]) -> BruteResult {
    let n = clauses
        .iter()
        .flatten()
        .fold(num_vars, |acc, l| acc.max(l.unsigned_abs() as usize));

    // 空子句直接不可满足。
    if clauses.iter().any(|c| c.is_empty()) {
        return BruteResult::Unsat;
    }

    let mut assignment = vec![false; n];
    loop {
        let mut all = true;
        for clause in clauses {
            let satisfied = clause.iter().any(|&lit| {
                let v = lit.unsigned_abs() as usize - 1;
                let truth = assignment[v];
                if lit > 0 { truth } else { !truth }
            });
            if !satisfied {
                all = false;
                break;
            }
        }
        if all {
            return BruteResult::Sat { assignment };
        }
        // 二进制 +1：最低位对应变量 1；全 1 再加一即枚举结束。
        let mut carry = true;
        for slot in assignment.iter_mut() {
            if carry {
                if !*slot {
                    *slot = true;
                    carry = false;
                    break;
                } else {
                    *slot = false;
                }
            }
        }
        if carry {
            return BruteResult::Unsat;
        }
    }
}

/// 朴素子句规范化（与产品代码独立的判定，仅用于构造 oracle 输入时
/// 去除重言式与重复，使暴力枚举与规范化语义一致）。
///
/// 注意：这里**保留**空子句；重言式整条不进入结果。
#[allow(dead_code)]
pub fn naive_strip(clauses: &[Vec<i64>]) -> Vec<Vec<i64>> {
    let mut out = Vec::new();
    for c in clauses {
        let mut uniq: Vec<i64> = Vec::new();
        let mut taut = false;
        for &l in c {
            if uniq.contains(&l) {
                continue;
            }
            if uniq.contains(&-l) {
                taut = true;
            }
            uniq.push(l);
        }
        if taut {
            continue;
        }
        out.push(uniq);
    }
    out
}

#[cfg(test)]
mod self_tests {
    use super::*;

    #[test]
    fn oracle_known_sat() {
        // (x1 ∨ x2) ∧ (¬x1 ∨ x2)：x2=true 可满足。
        let r = brute_force(2, &[vec![1, 2], vec![-1, 2]]);
        match r {
            BruteResult::Sat { assignment } => assert!(assignment[1]),
            BruteResult::Unsat => panic!("应为 SAT"),
        }
    }

    #[test]
    fn oracle_known_unsat() {
        let r = brute_force(1, &[vec![1], vec![-1]]);
        assert_eq!(r, BruteResult::Unsat);
    }

    #[test]
    fn oracle_empty_clause_is_unsat() {
        assert_eq!(
            brute_force(1, &[vec![], vec![1]]),
            BruteResult::Unsat
        );
    }
}
