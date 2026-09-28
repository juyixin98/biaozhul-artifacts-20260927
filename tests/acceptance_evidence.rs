//! 验收测试 2：篡改证据必须被**独立检查器**拒绝，并断言具体失败类别。
//!
//! 每个测试先让求解器产出合法证据，再做一种针对性篡改，随后断言检查器
//! 返回精确的错误枚举，而不是笼统的 Err。

mod common;

use cnf_dpll::evidence::checker::{check_model, check_proof, ModelError, ProofError};
use cnf_dpll::evidence::types::{
    ClauseRef, Model, Outcome, ProofStep, ResolutionProof,
};
use cnf_dpll::lit::{lit_from_signed, neg};
use cnf_dpll::normalize::normalize_signed_clauses;
use cnf_dpll::solver::{solve_normalized, Budget};

fn sat_model_of(n: usize, clauses: &[Vec<i64>]) -> Model {
    let cnf = normalize_signed_clauses(n, clauses);
    match solve_normalized(&cnf, &Budget::unlimited()).outcome {
        Outcome::Sat { model } => model,
        other => panic!("测试夹具应 SAT: {other:?}"),
    }
}

fn unsat_proof_of(n: usize, clauses: &[Vec<i64>]) -> ResolutionProof {
    let cnf = normalize_signed_clauses(n, clauses);
    match solve_normalized(&cnf, &Budget::unlimited()).outcome {
        Outcome::Unsat { proof } => proof,
        other => panic!("测试夹具应 UNSAT: {other:?}"),
    }
}

// ============================ 模型篡改 ============================

#[test]
fn tampered_model_flip_one_var_is_rejected() {
    // (x1∨x2) ∧ (¬x1∨x2)，真模型 x2=true。翻转 x1/x2 中的关键真值。
    let clauses = vec![vec![1i64, 2], vec![-1, 2]];
    let mut model = sat_model_of(2, &clauses);
    // 全部翻为假极性。
    model.true_literals = model
        .true_literals
        .iter()
        .map(|&l| neg(l))
        .collect();
    model.true_literals.sort_unstable();
    let err = check_model(2, &clauses, &model).unwrap_err();
    assert!(
        matches!(
            err,
            ModelError::ClauseNotSatisfied { idx: 0 | 1, .. }
        ),
        "翻转全部极性后至少一条子句不满足，实际: {err:?}"
    );
}

#[test]
fn truncated_model_missing_variable_is_rejected() {
    let clauses = vec![vec![1i64, 2], vec![-1, -2]];
    let mut model = sat_model_of(2, &clauses);
    model.true_literals.pop();
    let err = check_model(2, &clauses, &model).unwrap_err();
    assert!(
        matches!(err, ModelError::MissingVar { .. }),
        "缺变量应被拒绝: {err:?}"
    );
}

#[test]
fn model_with_conflicting_polarity_is_rejected() {
    let clauses: Vec<Vec<i64>> = vec![vec![1, 2]];
    let mut model = sat_model_of(2, &clauses);
    // 同时塞入 x1 与 ¬x1。
    model.true_literals.push(lit_from_signed(-1));
    let err = check_model(2, &clauses, &model).unwrap_err();
    assert!(
        matches!(err, ModelError::ConflictingVar { var: 1 }),
        "同变量双极性应报 ConflictingVar: {err:?}"
    );
}

#[test]
fn model_with_duplicate_literal_is_rejected() {
    let clauses: Vec<Vec<i64>> = vec![vec![1, 2]];
    let mut model = sat_model_of(2, &clauses);
    model.true_literals.push(lit_from_signed(1));
    let err = check_model(2, &clauses, &model).unwrap_err();
    assert!(
        matches!(err, ModelError::DuplicateVar { var: 1 }),
        "重复文字应报 DuplicateVar: {err:?}"
    );
}

#[test]
fn model_num_vars_field_tampered_is_rejected() {
    let clauses: Vec<Vec<i64>> = vec![vec![1, 2]];
    let mut model = sat_model_of(2, &clauses);
    model.num_vars = 1;
    let err = check_model(2, &clauses, &model).unwrap_err();
    assert!(
        matches!(err, ModelError::NumVarsMismatch { reported: 1, actual: 2 }),
        "num_vars 篡改必须被识别: {err:?}"
    );
}

#[test]
fn model_var_out_of_range_is_rejected() {
    let clauses: Vec<Vec<i64>> = vec![vec![1, 2]];
    let mut model = sat_model_of(2, &clauses);
    model.true_literals[0] = lit_from_signed(9);
    let err = check_model(2, &clauses, &model).unwrap_err();
    assert!(matches!(err, ModelError::VarOutOfRange { var: 9, .. }));
}

// ============================ 证明篡改 ============================

#[test]
fn tampered_proof_resolvent_is_rejected() {
    let clauses: Vec<Vec<i64>> = vec![
        vec![1, 2],
        vec![1, -2],
        vec![-1, 2],
        vec![-1, -2],
    ];
    let mut proof = unsat_proof_of(2, &clauses);
    // 翻转最后一步 resolvent：从空子句改成单文字。
    let last = proof.steps.last_mut().unwrap();
    last.resolvent = vec![lit_from_signed(1)];
    let err = check_proof(2, &clauses, &proof).unwrap_err();
    // 要么归结结果对不上，要么末步非空；两者都是具体类别。
    assert!(
        matches!(
            err,
            ProofError::ResolventMismatch { .. } | ProofError::FinalNotEmpty { .. }
        ),
        "resolvent 篡改必须被拒绝: {err:?}"
    );
}

#[test]
fn tampered_proof_pivot_is_rejected() {
    let clauses: Vec<Vec<i64>> = vec![
        vec![1, 2],
        vec![1, -2],
        vec![-1, 2],
        vec![-1, -2],
    ];
    let mut proof = unsat_proof_of(2, &clauses);
    // 找一个确实含枢轴的步骤，把枢轴改成别的变量。
    let step = proof
        .steps
        .iter_mut()
        .find(|s| !s.pivot_vars.is_empty())
        .expect("UNSAT 证明至少有一次归结");
    let original = step.pivot_vars[0];
    step.pivot_vars[0] = if original == 1 { 2 } else { 1 };
    let err = check_proof(2, &clauses, &proof).unwrap_err();
    assert!(
        matches!(
            err,
            ProofError::PivotMismatch { .. } | ProofError::NoPivot { .. }
        ),
        "枢轴篡改必须被识别: {err:?}"
    );
}

#[test]
fn proof_with_dangling_lemma_ref_is_rejected() {
    let clauses: Vec<Vec<i64>> = vec![
        vec![1, 2],
        vec![1, -2],
        vec![-1, 2],
        vec![-1, -2],
    ];
    let mut proof = unsat_proof_of(2, &clauses);
    // 把某步的边句改成一个不存在的引理号。
    let step = proof
        .steps
        .iter_mut()
        .find(|s| !s.side.is_empty())
        .unwrap();
    step.side[0] = ClauseRef::Lemma { id: 9999 };
    // pivot 数量与 side 数量一致仍成立；引用解析失败。
    let err = check_proof(2, &clauses, &proof).unwrap_err();
    assert!(
        matches!(err, ProofError::BadLemmaRef { id: 9999, .. }),
        "悬空引理引用必须拒绝: {err:?}"
    );
}

#[test]
fn proof_with_bad_input_index_is_rejected() {
    let clauses: Vec<Vec<i64>> = vec![
        vec![1, 2],
        vec![1, -2],
        vec![-1, 2],
        vec![-1, -2],
    ];
    let mut proof = unsat_proof_of(2, &clauses);
    proof.steps[0].main = ClauseRef::Input { idx: 400 };
    let err = check_proof(2, &clauses, &proof).unwrap_err();
    assert!(
        matches!(err, ProofError::BadInputRef { idx: 400, .. }),
        "越界输入引用必须拒绝: {err:?}"
    );
}

#[test]
fn proof_with_reordered_steps_is_rejected() {
    let clauses: Vec<Vec<i64>> = vec![
        vec![1, 2],
        vec![1, -2],
        vec![-1, 2],
        vec![-1, -2],
    ];
    let mut proof = unsat_proof_of(2, &clauses);
    if proof.steps.len() >= 2 {
        proof.steps.swap(0, 1);
        let err = check_proof(2, &clauses, &proof).unwrap_err();
        // 编号失序：要么 id 不连续，要么引用先于定义。
        assert!(
            matches!(
                err,
                ProofError::IdNotSequential { .. } | ProofError::BadLemmaRef { .. }
            ),
            "乱序步骤必须拒绝: {err:?}"
        );
    }
}

#[test]
fn empty_proof_is_rejected_even_if_formula_unsat() {
    let clauses: Vec<Vec<i64>> = vec![vec![1], vec![-1]];
    let fake = ResolutionProof { steps: vec![] };
    let err = check_proof(1, &clauses, &fake).unwrap_err();
    assert_eq!(err, ProofError::EmptyProof);
}

#[test]
fn proof_step_with_no_pivot_pair_is_rejected() {
    // 手工构造一步：两条毫不相干的子句做归结，检查器必须报 NoPivot。
    let clauses: Vec<Vec<i64>> = vec![vec![1, 2], vec![3, 4]];
    let step = ProofStep {
        id: 0,
        main: ClauseRef::Input { idx: 0 },
        side: vec![ClauseRef::Input { idx: 1 }],
        pivot_vars: vec![1],
        resolvent: vec![
            lit_from_signed(1),
            lit_from_signed(2),
            lit_from_signed(3),
            lit_from_signed(4),
        ],
    };
    let proof = ResolutionProof { steps: vec![step] };
    let err = check_proof(4, &clauses, &proof).unwrap_err();
    assert!(
        matches!(err, ProofError::NoPivot { step: 0, at: 0 }),
        "无互补对必须报 NoPivot: {err:?}"
    );
}

#[test]
fn valid_generated_proofs_pass_independent_checker_on_php() {
    // PHP-3-2 的多步证明必须通过（确保上面的拒绝不是因为检查器过严）。
    let (p, h) = (3usize, 2usize);
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
    let proof = unsat_proof_of(p * h, &clauses);
    check_proof(p * h, &clauses, &proof)
        .unwrap_or_else(|e| panic!("合法 PHP 证明应通过: {e}"));
    assert!(
        proof.steps.len() >= 2,
        "PHP-3-2 应产生多步推导（含中间引理与空末步），实际 {} 步",
        proof.steps.len()
    );
    assert!(
        proof.steps.last().unwrap().resolvent.is_empty(),
        "最后一步必须是空子句"
    );
}
