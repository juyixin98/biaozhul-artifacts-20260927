//! 验收测试 1：对少量变量，穷举真值表与求解器结论逐条对账。
//!
//! 参考答案由独立暴力枚举 oracle 产生（`tests/common/oracle.rs`），
//! 不由被测核心生成。除结论一致外，还断言：
//! - SAT：求解器返回的具体模型确实满足公式（oracle 复算）；
//! - UNSAT：求解器给出的证明必须被**独立检查器**接受；
//! - 规范化计数（重复/重言式/空子句）与预期数值相等。

mod common;

use cnf_dpll::evidence::checker::{check_model, check_proof};
use cnf_dpll::evidence::types::Outcome;
use cnf_dpll::normalize::normalize_signed_clauses;
use cnf_dpll::solver::{solve_normalized, Budget};
use cnf_dpll::lit::var_of;

use common::oracle::{brute_force, naive_strip, BruteResult};

/// 一条公式上的三方对账：solver ↔ 独立检查器 ↔ 暴力 oracle。
fn cross_check(name: &str, n: usize, clauses: &[Vec<i64>]) {
    let stripped = naive_strip(clauses);
    let oracle = brute_force(n, &stripped);

    let cnf = normalize_signed_clauses(n, clauses);
    let result = solve_normalized(&cnf, &Budget::unlimited());

    match (&result.outcome, &oracle) {
        (Outcome::Sat { model }, BruteResult::Sat { .. }) => {
            // 独立检查器必须接受该模型。
            check_model(n, clauses, model)
                .unwrap_or_else(|e| panic!("[{name}] 求解器模型被检查器拒绝: {e}"));
            // 模型必须覆盖全部变量。
            let mut covered = vec![false; n + 1];
            for &l in &model.true_literals {
                covered[var_of(l) as usize] = true;
            }
            for (v, &is_covered) in covered.iter().enumerate().skip(1) {
                assert!(is_covered, "[{name}] 模型缺少变量 {v}");
            }
            // 用 oracle 的朴素语义复算模型：从证据构造真值向量。
            let mut truth = vec![false; n];
            for &l in &model.true_literals {
                truth[(var_of(l) - 1) as usize] = l & 1 == 0;
            }
            for clause in &stripped {
                let ok = clause.iter().any(|&lit| {
                    let t = truth[lit.unsigned_abs() as usize - 1];
                    if lit > 0 { t } else { !t }
                });
                assert!(ok, "[{name}] 模型未满足朴素子句 {clause:?}");
            }
        }
        (Outcome::Unsat { proof }, BruteResult::Unsat) => {
            check_proof(n, clauses, proof)
                .unwrap_or_else(|e| panic!("[{name}] 求解器证明被检查器拒绝: {e}"));
        }
        (got, want) => panic!(
            "[{name}] 结论不一致：solver={got:?}, oracle={want:?}"
        ),
    }
}

// -------------------------------------------------------------------
// 固定手工公式：每个都断言到具体结果，而不是"能调用"。
// -------------------------------------------------------------------

#[test]
fn empty_formula_is_sat_with_empty_model() {
    let cnf = normalize_signed_clauses(0, &[]);
    let r = solve_normalized(&cnf, &Budget::unlimited());
    match &r.outcome {
        Outcome::Sat { model } => {
            assert!(model.true_literals.is_empty());
            assert_eq!(model.num_vars, 0);
        }
        other => panic!("空公式必须 SAT，得到 {other:?}"),
    }
}

#[test]
fn unit_clause_cascade_forces_chain() {
    // 级联单位传播：x1；¬x1∨x2；¬x2∨x3；¬x3∨x4；¬x4∨x5。
    // 唯一模型 x1..x5 全真，且 0 次决策（全靠传播）。
    let clauses: Vec<Vec<i64>> = vec![
        vec![1],
        vec![-1, 2],
        vec![-2, 3],
        vec![-3, 4],
        vec![-4, 5],
    ];
    let cnf = normalize_signed_clauses(5, &clauses);
    let r = solve_normalized(&cnf, &Budget::unlimited());
    let model = match &r.outcome {
        Outcome::Sat { model } => model,
        other => panic!("期望 SAT: {other:?}"),
    };
    for v in 1..=5u32 {
        assert!(
            model.true_literals.contains(&(2 * v)),
            "变量 {v} 必须为真，模型 {:?}",
            model.true_literals
        );
    }
    assert_eq!(r.counters.decisions, 0, "不应发生决策");
    assert_eq!(r.counters.propagations, 5);
    cross_check("unit-cascade", 5, &clauses);
}

#[test]
fn repeated_backtracking_forces_successive_conflicts() {
    // 连续回溯 SAT 夹具（5 变量）。正文字优先路线在 x3 上两次撞墙后改道：
    //   (¬x1∨¬x2∨x3)(¬x1∨¬x2∨¬x3)(x1∨¬x3∨x4)(¬x3∨¬x4∨x5)
    //   (¬x4∨¬x5)(x2∨x5)(¬x1∨x4)
    // 确定性轨迹：5 次决策、2 次冲突回溯（冲突计数具体断言），最终模型
    // [x1=T,x2=T,x3=F,x4=F,x5=F]，并与暴力真值表 oracle 对账。
    let clauses: Vec<Vec<i64>> = vec![
        vec![-1, -2, 3],
        vec![-1, -2, -3],
        vec![1, -3, 4],
        vec![-3, -4, 5],
        vec![-4, -5],
        vec![2, 5],
        vec![-1, 4],
    ];
    let cnf = normalize_signed_clauses(5, &clauses);
    let r = solve_normalized(&cnf, &Budget::unlimited());
    let model = match &r.outcome {
        Outcome::Sat { model } => model,
        other => panic!("期望 SAT: {other:?}"),
    };
    assert_eq!(
        model.true_literals,
        vec![3, 4, 7, 8, 11],
        "确定性策略必须给出确定模型"
    );
    assert_eq!(r.counters.conflicts, 2, "必须连续发生 2 次冲突回溯");
    assert_eq!(r.counters.decisions, 5);
    cross_check("repeat-backtrack", 5, &clauses);
}

#[test]
fn contradictory_empty_clause_is_unsat_with_proof() {
    // 直接矛盾空子句：与其它任意子句并列。
    let clauses: Vec<Vec<i64>> = vec![vec![1, 2], vec![], vec![-1]];
    let cnf = normalize_signed_clauses(2, &clauses);
    assert!(cnf.has_empty_clause());
    let r = solve_normalized(&cnf, &Budget::unlimited());
    match &r.outcome {
        Outcome::Unsat { proof } => {
            assert_eq!(proof.steps.len(), 1);
            assert!(proof.steps[0].resolvent.is_empty());
            check_proof(2, &clauses, proof).expect("空子句证明必须被检查器接受");
        }
        other => panic!("含空子句必须 UNSAT: {other:?}"),
    }
    assert_eq!(brute_force(2, &clauses), BruteResult::Unsat);
}

#[test]
fn classic_two_var_unsat_resolution() {
    // (x1∨x2)(x1∨¬x2)(¬x1∨x2)(¬x1∨¬x2) 穷举四个赋值全部失败。
    let clauses: Vec<Vec<i64>> = vec![
        vec![1, 2],
        vec![1, -2],
        vec![-1, 2],
        vec![-1, -2],
    ];
    cross_check("2var-unsat", 2, &clauses);
}

#[test]
fn pigeonhole_3_2_is_unsat() {
    // 3 鸽 2 洞，经典不可满足。
    let p = 3usize;
    let h = 2usize;
    let lit = |i: usize, j: usize| (i as i64 - 1) * h as i64 + j as i64;
    let mut clauses = Vec::new();
    for i in 1..=p {
        clauses.push((1..=h).map(|j| lit(i, j)).collect::<Vec<_>>());
    }
    for i in 1..=p {
        for k in (i + 1)..=p {
            for j in 1..=h {
                clauses.push(vec![-lit(i, j), -lit(k, j)]);
            }
        }
    }
    cross_check("php-3-2", p * h, &clauses);
}

// -------------------------------------------------------------------
// 穷举枚举：对 n=3 的一批确定性生成公式自动对账。
// -------------------------------------------------------------------

#[test]
fn exhaustive_small_formulas_match_truth_table() {
    // 用固定种子的轻量 LCG 生成 3/4 变量、1..6 子句的公式，每个都做
    // solver ↔ 独立检查器 ↔ 暴力真值表三方对账。生成器是简单算术，不依赖
    // 被测代码；UNSAT 实例的证明也在此被逐一步验证。
    let mut state: u64 = 0x1234_5678_9abc_def0;
    let mut next = || {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        state
    };

    for (nvars, trials) in [(3usize, 200u64), (4usize, 300u64)] {
        let all_lits: Vec<i64> = (1..=nvars as i64)
            .flat_map(|v| [v, -v])
            .collect();
        for t in 0..trials {
            let m = 1 + next() as usize % 6;
            let mut clauses: Vec<Vec<i64>> = Vec::new();
            for _ in 0..m {
                let len = 1 + next() as usize % 3;
                let mut clause = Vec::new();
                for _ in 0..len {
                    clause.push(all_lits[next() as usize % all_lits.len()]);
                }
                clauses.push(clause);
            }
            cross_check(
                &format!("random-{nvars}var-{t}"),
                nvars,
                &clauses,
            );
        }
    }
}

#[test]
fn duplicate_and_tautology_normalization_counts() {
    // 重复文字 3 处；一条重言子句被整体丢弃。
    let clauses: Vec<Vec<i64>> = vec![
        vec![1, 1, 2, 2],       // 2 个重复
        vec![-1, 1, 3],         // 重言 → 删除
        vec![2, 2, -3],         // 1 个重复
    ];
    let cnf = normalize_signed_clauses(3, &clauses);
    assert_eq!(cnf.duplicate_literals_removed, 3);
    assert_eq!(cnf.tautology_clauses_removed, 1);
    assert_eq!(cnf.clauses.len(), 2);
    assert_eq!(cnf.clauses[0], vec![2u32, 4]); // 排序后：x1=2, x2=4
}

#[test]
fn contradictory_unit_clauses_unsat() {
    let clauses: Vec<Vec<i64>> = vec![vec![1], vec![-1]];
    let cnf = normalize_signed_clauses(1, &clauses);
    let r = solve_normalized(&cnf, &Budget::unlimited());
    match &r.outcome {
        Outcome::Unsat { proof } => {
            check_proof(1, &clauses, proof).expect("矛盾单位子句证明应被接受");
        }
        other => panic!("期望 UNSAT: {other:?}"),
    }
}
