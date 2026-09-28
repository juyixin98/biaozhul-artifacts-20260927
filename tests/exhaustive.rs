//! 更大规模的穷举交叉验证：所有 3 变量、至多 3 条子句的公式，
//! 以及 4 变量的固定抽样。这组测试耗时可控，但能覆盖
//! 级联传播、连续多层回退、学习子句复用等路径。

#[path = "common/oracle.rs"]
mod oracle;

use std::time::{Duration, Instant};

use cnf_solver_backend::cnf::normalize_formula;
use cnf_solver_backend::solver::{SolveLimits, Solver, Status};
use cnf_solver_backend::verify::{check_model, check_proof};

use oracle::{brute_force, OracleVerdict};

mod helper {
    use cnf_solver_backend::cnf::Lit;
    pub fn universe(n: usize) -> Vec<Vec<Lit>> {
        let states = 3u64.pow(n as u32);
        let mut out = Vec::new();
        for s in 0..states {
            let mut code = s;
            let mut clause = Vec::new();
            for v in 1..=n {
                match code % 3 {
                    0 => clause.push(v as Lit),
                    1 => clause.push(-(v as Lit)),
                    _ => {}
                }
                code /= 3;
            }
            out.push(clause);
        }
        out
    }
}
use helper::universe;

fn check_one(raw: &[Vec<i32>]) {
    let (formula, _) = normalize_formula(None, raw).unwrap();
    let expected = brute_force(&formula);
    let out = Solver::new(&formula).solve(&SolveLimits::default());
    match expected {
        OracleVerdict::Sat => {
            assert_eq!(out.status, Status::Sat);
            check_model(&formula, out.model.as_ref().unwrap()).unwrap();
        }
        OracleVerdict::Unsat => {
            assert_eq!(out.status, Status::Unsat);
            check_proof(&formula, out.proof.as_ref().unwrap()).unwrap();
        }
    }
}

#[test]
fn exhaustive_three_vars_up_to_three_clauses() {
    let u = universe(3); // 27 条候选
    let start = Instant::now();
    let mut count = 0u64;
    // 空公式与单子句（27）
    check_one(&[]);
    count += 1;
    for a in &u {
        check_one(std::slice::from_ref(a));
        count += 1;
    }
    // 全部两两组合（含重复，覆盖规范化）：27^2 = 729
    for a in &u {
        for b in &u {
            check_one(&[a.clone(), b.clone()]);
            count += 1;
        }
    }
    // 三三元组合：27^3 ≈ 2 万，仍然亚秒级。
    for a in &u {
        for b in &u {
            for c in &u {
                check_one(&[a.clone(), b.clone(), c.clone()]);
                count += 1;
            }
        }
    }
    assert!(
        start.elapsed() < Duration::from_secs(30),
        "exhaustive suite too slow"
    );
    assert_eq!(count, 1 + 27 + 729 + 27 * 27 * 27);
}

#[test]
fn sampled_four_var_formulas() {
    let u = universe(4); // 81 条候选
                         // 确定性 LCG 抽样 600 个 6 子句公式。
    let mut state: u64 = 0xdead_beef_cafe_babe;
    let mut next = || {
        state = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        state
    };
    for _ in 0..600 {
        let mut raw = Vec::new();
        for _ in 0..6 {
            let idx = (next() % u.len() as u64) as usize;
            raw.push(u[idx].clone());
        }
        check_one(&raw);
    }
    // 几个手工构造：纯传播链 + 连续回退 + 空子句混合。
    check_one(&[
        vec![1],
        vec![-1, 2],
        vec![-2, 3],
        vec![-3, 4],
        vec![-4, -1], // 传播到底后矛盾（x1 已真）
    ]);
}
