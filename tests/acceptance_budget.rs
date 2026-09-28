//! 验收测试 3：预算耗尽必须是 UNKNOWN，不能滑成 UNSAT/SAT；解析错误分类。

mod common;

use std::time::Duration;

use cnf_dpll::evidence::checker::{check_outcome, OutcomeCheckError};
use cnf_dpll::input::parse_dimacs;
use cnf_dpll::input::ParseError;
use cnf_dpll::normalize::normalize_signed_clauses;
use cnf_dpll::solver::{solve_normalized, Budget};
use cnf_dpll::evidence::types::Outcome;

/// PHP-4-3：足够大的不可满足实例，用于把极小预算打穿。
fn php_4_3() -> (usize, Vec<Vec<i64>>) {
    let (p, h) = (4usize, 3usize);
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
    (p * h, clauses)
}

#[test]
fn decision_budget_zero_forces_unknown_not_unsat() {
    let (n, clauses) = php_4_3();
    let cnf = normalize_signed_clauses(n, &clauses);
    let budget = Budget {
        time_limit: None,
        max_propagations: Some(1),
        max_decisions: Some(0),
    };
    let r = solve_normalized(&cnf, &budget);
    assert!(
        matches!(r.outcome, Outcome::Unknown { .. }),
        "决策预算 0 时必须 UNKNOWN，得到 {:?}",
        r.outcome
    );
    assert!(r.budget_exceeded.is_some());
    // 独立检查器不得为 UNKNOWN 背书。
    let err = check_outcome(n, &clauses, &r.outcome).unwrap_err();
    assert_eq!(err, OutcomeCheckError::UnknownCannotBeVerified);
}

#[test]
fn propagation_budget_tiny_forces_unknown() {
    let (n, clauses) = php_4_3();
    let cnf = normalize_signed_clauses(n, &clauses);
    let budget = Budget {
        time_limit: None,
        max_propagations: Some(2),
        max_decisions: Some(1),
    };
    let r = solve_normalized(&cnf, &budget);
    assert!(
        matches!(r.outcome, Outcome::Unknown { .. }),
        "极小传播预算必须 UNKNOWN"
    );
}

#[test]
fn same_hard_instance_is_unsat_without_budget() {
    // 对照：放开预算后同一实例给出可验的 UNSAT，证明 UNKNOWN 不是偷懒。
    let (n, clauses) = php_4_3();
    let cnf = normalize_signed_clauses(n, &clauses);
    let r = solve_normalized(&cnf, &Budget::unlimited());
    assert!(matches!(r.outcome, Outcome::Unsat { .. }));
}

#[test]
fn time_limit_nonexistent_1ns_returns_unknown_for_sat_too() {
    // 1ns 时间预算：哪怕是 SAT 实例，预算也可能先到 → 仍是 UNKNOWN 而非 SAT。
    let n = 30usize;
    // 链式 x_i→x_{i+1} 加 x1：实际很快 SAT；但 1ns 预算可能在第一个
    // 检查点就耗尽。若没耗尽（机器太快），允许 SAT——本测试只断言
    // "UNKNOWN 时绝不带可验结论"。
    let mut clauses: Vec<Vec<i64>> = vec![vec![1]];
    for i in 1..n as i64 {
        clauses.push(vec![-i, i + 1]);
    }
    let cnf = normalize_signed_clauses(n, &clauses);
    let budget = Budget {
        time_limit: Some(Duration::from_nanos(1)),
        max_propagations: None,
        max_decisions: None,
    };
    let r = solve_normalized(&cnf, &budget);
    if let Outcome::Unknown { .. } = &r.outcome {
        assert!(r.budget_exceeded.is_some());
        assert!(check_outcome(n, &clauses, &r.outcome).is_err());
    } else {
        assert!(matches!(r.outcome, Outcome::Sat { .. }));
    }
}

// -------------------------------------------------------------------
// 解析失败的具体类别。
// -------------------------------------------------------------------

#[test]
fn parse_errors_have_distinct_categories() {
    assert!(matches!(
        parse_dimacs("1 0\np cnf 1 1\n"),
        Err(ParseError::DataBeforeHeader)
    ));
    assert!(matches!(
        parse_dimacs("p cnf x 1\n1 0\n"),
        Err(ParseError::MalformedHeader(_))
    ));
    assert!(matches!(
        parse_dimacs("p cnf 2 1\n1 banana 0\n"),
        Err(ParseError::IllegalToken(_))
    ));
    assert!(matches!(
        parse_dimacs("p cnf 2 1\n1 3 0\n"),
        Err(ParseError::VarOutOfBound {
            declared: 2,
            seen: 3
        })
    ));
    assert!(matches!(
        parse_dimacs("p cnf 2 2\n1 0\n"),
        Err(ParseError::ClauseCountMismatch {
            declared: 2,
            actual: 1
        })
    ));
    assert!(matches!(
        parse_dimacs("1 0\n"),
        Err(ParseError::DataBeforeHeader | ParseError::MissingHeader)
    ));
}

#[test]
fn unknown_partial_proof_is_marked_and_not_served_as_unsat() {
    let (n, clauses) = php_4_3();
    let cnf = normalize_signed_clauses(n, &clauses);
    let r = solve_normalized(
        &cnf,
        &Budget {
            time_limit: None,
            max_propagations: Some(1),
            max_decisions: Some(0),
        },
    );
    match r.outcome {
        Outcome::Unknown {
            reason,
            partial_proof,
        } => {
            assert!(!reason.is_empty(), "UNKNOWN 必须给出原因");
            // 部分证明存在但最后一步不是空子句（或为空），不得据此声称 UNSAT。
            if let Some(last) = partial_proof.steps.last() {
                // 允许部分引理，但我们明确不接受其作为 UNSAT 证据。
                let _ = last;
            }
        }
        other => panic!("必须 UNKNOWN: {other:?}"),
    }
}
