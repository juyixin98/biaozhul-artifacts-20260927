//! 独立参考预言机：穷举真值表。
//!
//! 这是集成测试专用的“第三方裁判”，与求解内核没有任何共享实现
//! （只复用输入/规范化这一层公共解析，不碰 `solver` 模块）。
//! 对小公式，它的判定就是 ground truth。

use cnf_solver_backend::cnf::{normalize_formula, Formula, Lit};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OracleVerdict {
    Sat,
    Unsat,
}

/// 穷举 2^n 个赋值。n 很小时成本可忽略。
pub fn brute_force(formula: &Formula) -> OracleVerdict {
    for bits in 0u64..(1u64 << formula.num_vars) {
        let mut model = vec![false; formula.num_vars + 1];
        for (v, slot) in model.iter_mut().enumerate().skip(1) {
            *slot = (bits >> (v - 1)) & 1 == 1;
        }
        if satisfies(formula, &model) {
            return OracleVerdict::Sat;
        }
    }
    OracleVerdict::Unsat
}

fn satisfies(formula: &Formula, model: &[bool]) -> bool {
    formula.clauses.iter().all(|c| {
        c.lits.iter().any(|&l| {
            let v = l.unsigned_abs() as usize;
            if l > 0 {
                model[v]
            } else {
                !model[v]
            }
        })
    })
}

/// 直接从原始子句构造规范化公式（测试夹具便捷函数；并非每个测试二进制都使用）。
#[allow(dead_code)]
pub fn prepare(raw: &[Vec<Lit>]) -> Formula {
    normalize_formula(None, raw).unwrap().0
}
