//! 独立证据验证模块测试：断言具体结论与失败类别。
//!
//! 该模块绕过求解内核，直接从输入语言重算图边，因此这些用例的参考答案
//! 完全不依赖 Bellman-Ford 的实现。

mod common;

use std::collections::BTreeMap;

use common::*;
use diff_constraints_service::evidence::{verify_cycle, VerifyError};
use diff_constraints_service::model::ConstraintInput;

fn by_name(cs: &[ConstraintInput]) -> BTreeMap<String, ConstraintInput> {
    cs.iter().map(|k| (k.name.clone(), k.clone())).collect()
}

#[test]
fn valid_strict_negative_two_cycle_accepted() {
    let log = RunLog::new("valid_strict_negative_two_cycle_accepted");
    let cs = vec![c("n1", "p", "q", -1), c("n2", "q", "p", -1)];
    let result = verify_cycle(&["n1".into(), "n2".into()], &by_name(&cs));
    let dto = result
        .expect("no request/state/computation error")
        .expect("closed strictly negative cycle");
    log.state("total_cost", dto.total_cost);
    assert_eq!(dto.total_cost, -2);
    assert_eq!(dto.cycle.len(), 2);
    assert_eq!(dto.cycle[0].constraint, "n1");
    assert_eq!(
        (dto.cycle[0].from.as_str(), dto.cycle[0].to.as_str()),
        ("q", "p")
    );
    assert_eq!(
        (dto.cycle[1].from.as_str(), dto.cycle[1].to.as_str()),
        ("p", "q")
    );
    log.verdict(
        "valid",
        "edges re-derived from input language; cycle closed, cost -2 < 0",
    );
}

#[test]
fn unclosed_chain_is_rejected_with_reason() {
    let log = RunLog::new("unclosed_chain_is_rejected_with_reason");
    let cs = vec![c("n1", "p", "q", -1)]; // 单边 q->p，不闭合
    let result = verify_cycle(&["n1".into()], &by_name(&cs))
        .expect("request is well-formed")
        .expect_err("single non-self edge cannot be a cycle");
    log.state("reason", &result);
    assert!(
        result.contains("not closed"),
        "reason must explain non-closure: {result}"
    );
    log.verdict(
        "invalid",
        "edge ends at p but the next edge starts at q -> not closed",
    );
}

#[test]
fn zero_cost_cycle_rejected_as_not_strictly_negative() {
    let log = RunLog::new("zero_cost_cycle_rejected_as_not_strictly_negative");
    let cs = vec![c("z1", "x", "y", 0), c("z2", "y", "x", 0)];
    let result = verify_cycle(&["z1".into(), "z2".into()], &by_name(&cs))
        .expect("request is well-formed")
        .expect_err("zero-cost cycle is not a conflict");
    log.state("reason", &result);
    assert!(result.contains("not strictly negative"), "reason: {result}");
    assert!(result.contains('0'));
    log.verdict(
        "invalid",
        "cycle closed but cost 0 >= 0; feasibility is not refuted",
    );
}

#[test]
fn positive_cycle_rejected() {
    let log = RunLog::new("positive_cycle_rejected");
    let cs = vec![c("p1", "a", "b", 1), c("p2", "b", "a", 1)];
    let result = verify_cycle(&["p1".into(), "p2".into()], &by_name(&cs))
        .expect("request is well-formed")
        .expect_err("positive cycle is not a conflict");
    log.state("reason", &result);
    assert!(result.contains("not strictly negative") && result.contains('2'));
    log.verdict("invalid", "cost 1+1=2 is non-negative");
}

#[test]
fn unknown_constraint_is_state_like_error() {
    let log = RunLog::new("unknown_constraint_is_state_like_error");
    let cs = vec![c("n1", "p", "q", -1)];
    let err = verify_cycle(&["n1".into(), "ghost".into()], &by_name(&cs))
        .expect_err("ghost constraint is absent");
    assert_eq!(err, VerifyError::UnknownConstraint("ghost".to_string()));
    log.state("error", &err);
    log.verdict(
        "error",
        "cycle references an id that is not in the set -> UnknownConstraint",
    );
}

#[test]
fn empty_and_repeating_cycle_are_input_errors() {
    let log = RunLog::new("empty_and_repeating_cycle_are_input_errors");
    let cs = vec![c("n1", "p", "q", -1), c("n2", "q", "p", -1)];
    let set = by_name(&cs);

    let empty = verify_cycle(&[], &set).expect_err("empty cycle is an input error");
    assert!(matches!(empty, VerifyError::Input(_)));

    let repeat =
        verify_cycle(&["n1".into(), "n1".into()], &set).expect_err("repeat is an input error");
    assert!(matches!(repeat, VerifyError::Input(ref m) if m.contains("repeats")));
    log.state("empty", &empty);
    log.state("repeat", &repeat);
    log.verdict(
        "input_error",
        "empty cycle and repeated ids rejected before graph reasoning",
    );
}

#[test]
fn cycle_cost_overflow_is_computation_error() {
    let log = RunLog::new("cycle_cost_overflow_is_computation_error");
    // 闭合二边环，但费用和为 MIN+MIN 溢出：不是“非负”，而是无法计算 -> 溢出错误。
    let cs = vec![
        c("o1", "a", "b", i64::MIN), // b->a, MIN
        c("o2", "b", "a", i64::MIN), // a->b, MIN；与 o1 闭合
    ];
    let err =
        verify_cycle(&["o1".into(), "o2".into()], &by_name(&cs)).expect_err("sum must overflow");
    assert!(
        matches!(err, VerifyError::ArithmeticOverflow(_)),
        "got {err:?}"
    );
    log.state("error", &err);
    log.verdict(
        "computation_failure",
        "checked_add overflows while summing cycle weights",
    );
}
