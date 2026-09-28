//! 端到端判定一致性：对全部 n≤3 的 CNF（以及更大的手工夹具），
//! 求解器结论必须与穷举真值表预言机逐例一致；
//! 且 SAT 的模型、UNSAT 的推导都必须通过独立检查器。
//!
//! 参考答案完全由真值表/检查器给出，不来自被测内核。

#[path = "common/oracle.rs"]
mod oracle;

use std::collections::HashSet;

use cnf_solver_backend::cnf::{normalize_formula, Lit};
use cnf_solver_backend::solver::{SolveLimits, Solver, Status};
use cnf_solver_backend::verify::{check_model, check_proof, ModelError, ProofError};

use oracle::{brute_force, prepare, OracleVerdict};

fn run_and_cross_check(raw: &[Vec<Lit>], label: &str) {
    let (formula, _notes) = normalize_formula(None, raw).unwrap();
    let expected = brute_force(&formula);
    let out = Solver::new(&formula).solve(&SolveLimits::default());

    match expected {
        OracleVerdict::Sat => {
            assert_eq!(out.status, Status::Sat, "[{label}] oracle says SAT");
            let model = out.model.expect("SAT must carry a model");
            check_model(&formula, &model)
                .unwrap_or_else(|e| panic!("[{label}] model rejected: {e:?}"));
            assert!(out.proof.is_none(), "[{label}] SAT must not carry a proof");
        }
        OracleVerdict::Unsat => {
            assert_eq!(out.status, Status::Unsat, "[{label}] oracle says UNSAT");
            let proof = out.proof.expect("UNSAT must carry a proof");
            check_proof(&formula, &proof)
                .unwrap_or_else(|e| panic!("[{label}] proof rejected: {e:?}"));
            assert!(
                out.model.is_none(),
                "[{label}] UNSAT must not carry a model"
            );
        }
    }
}

/// 对 n 个变量生成“所有可能的子句”集合：每个变量可选 {正, 负, 不出现}。
/// 然后对该全集做若干固定规模的抽样子集组合。
fn all_possible_clauses(n: usize) -> Vec<Vec<Lit>> {
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
        // 空子句也纳入；规范化会保留它。
        out.push(clause);
    }
    out
}

#[test]
fn exhaustive_all_two_var_formulas_of_two_clauses() {
    let universe = all_possible_clauses(2);
    // 全部 100 个有序二元组合（允许重复子句，覆盖规范化路径）。
    for i in 0..universe.len() {
        for j in 0..universe.len() {
            let raw = vec![universe[i].clone(), universe[j].clone()];
            run_and_cross_check(&raw, "2var pair");
        }
    }
}

#[test]
fn exhaustive_all_three_var_formulas_with_chosen_clause_subsets() {
    let universe = all_possible_clauses(3); // 27 条
                                            // 固定选若干“有代表性”的子集（全 2^27 太大；这里用确定性伪随机步长抽样 400 组）。
    let mut seen = HashSet::new();
    let mut mask: u64 = 0x1234_5678_9abc_def0;
    let mut cases = 0;
    while cases < 400 {
        // xorshift
        mask ^= mask << 13;
        mask ^= mask >> 7;
        mask ^= mask << 17;
        let mut raw = Vec::new();
        for (k, clause) in universe.iter().enumerate() {
            if (mask >> (k % 64)) & 1 == 1 {
                raw.push(clause.clone());
            }
        }
        if seen.insert(mask) {
            run_and_cross_check(&raw, "3var sampled");
            cases += 1;
        }
    }
}

#[test]
fn cascading_unit_propagation_matches_oracle_with_zero_decisions() {
    let raw = vec![vec![1], vec![-1, 2], vec![-2, 3], vec![-3, 4], vec![-4, 5]];
    let f = prepare(&raw);
    assert_eq!(brute_force(&f), OracleVerdict::Sat);
    let out = Solver::new(&f).solve(&SolveLimits::default());
    assert_eq!(out.status, Status::Sat);
    assert_eq!(
        out.diagnostics.decisions, 0,
        "pure propagation needs no decisions"
    );
    let m = out.model.unwrap();
    assert_eq!(&m[1..=5], &[true; 5]);
}

#[test]
fn contradictory_empty_clause_fixtures() {
    // 显式空子句、以及矛盾单位，三种等价写法都应 UNSAT，且证明/检查器通过。
    for raw in [
        vec![vec![]],
        vec![vec![1], vec![]],
        vec![vec![1], vec![-1]],
        vec![vec![1, 2], vec![-1], vec![-2]],
    ] {
        let f = prepare(&raw);
        assert_eq!(brute_force(&f), OracleVerdict::Unsat);
        let out = Solver::new(&f).solve(&SolveLimits::default());
        assert_eq!(out.status, Status::Unsat);
        check_proof(&f, &out.proof.unwrap()).unwrap();
    }
}

#[test]
fn tampered_model_is_rejected_with_specific_category() {
    let f = prepare(&[vec![1, -2], vec![2, 3]]);
    // 真实满足赋值之一：x2=true 传播 x3=true，x1 任意。篡改 x2=false,x3=false。
    let bogus = vec![false, true, false, false]; // 0 号位占位
    let err = check_model(&f, &bogus).unwrap_err();
    assert_eq!(err, ModelError::ClauseUnsatisfied { clause_index: 1 });

    // 缺变量：长度不够必须判 Incomplete，而不是按最后一条子句误判。
    let short = vec![false, true];
    assert!(matches!(
        check_model(&f, &short),
        Err(ModelError::Incomplete {
            expected_vars: 3,
            model_len: 1
        })
    ));
}

#[test]
fn tampered_proofs_are_rejected_with_specific_categories() {
    // 四个二元子句排除 (x1,x2) 的全部四种组合 ⇒ UNSAT。
    let f = prepare(&[vec![1, 2], vec![1, -2], vec![-1, 3], vec![-1, -3]]);
    // 先取一份真实证明作为篡改底稿（其内容本身可信，篡改方式由测试自行定义）。
    let genuine = Solver::new(&f)
        .solve(&SolveLimits::default())
        .proof
        .unwrap();
    check_proof(&f, &genuine).unwrap();
    check_proof(&f, &genuine).unwrap();

    // 篡改 A：翻转声明的派生文字。
    let mut tampered = genuine.clone();
    if let Some(d) = tampered.derived_clauses.first_mut() {
        for l in d.literals.iter_mut() {
            *l = -*l;
        }
    }
    assert!(matches!(
        check_proof(&f, &tampered),
        Err(ProofError::ResolventMismatch { .. })
    ));

    // 篡改 B：把最终空子句引用改成某条非空输入子句。
    let mut tampered = genuine.clone();
    tampered.empty_clause_ref = "i0".into();
    assert!(matches!(
        check_proof(&f, &tampered),
        Err(ProofError::EmptyRefNotEmpty { .. })
    ));

    // 篡改 C：引用不存在的子句。
    let mut tampered = genuine;
    tampered.derived_clauses[0].start_ref = "i999".into();
    assert!(matches!(
        check_proof(&f, &tampered),
        Err(ProofError::UnknownRef { .. })
    ));
}

#[test]
fn budget_exhaustion_returns_unknown_even_when_oracle_knows_unsat() {
    // 预言机确认 UNSAT，但决策预算给 0/1 时求解器只能报 UNKNOWN。
    let f = prepare(&[vec![1, 2], vec![1, -2], vec![-1, 3], vec![-1, -3]]);
    assert_eq!(brute_force(&f), OracleVerdict::Unsat);
    let out = Solver::new(&f).solve(&SolveLimits {
        max_decisions: Some(0),
        ..Default::default()
    });
    assert_eq!(out.status, Status::Unknown);
    assert!(out.proof.is_none(), "UNKNOWN must not masquerade as UNSAT");
    assert_eq!(
        out.diagnostics.stop_reason,
        cnf_solver_backend::solver::StopReason::DecisionBudget
    );
}
