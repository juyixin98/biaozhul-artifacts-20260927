//! Evidence-verification hardening: a tampered or stale counterexample must be
//! rejected, and rejection prevents a violation verdict.

use std::collections::BTreeMap;

use se_integration::common::{parse, run_with_z3, ASSERT_FAIL, DIV_BY_ZERO};
use se_lang::interp::RunOpts;
use se_verify::replay::{normalize_inputs, verify_evidence};

#[test]
fn out_of_domain_witness_is_rejected() {
    let program = parse(ASSERT_FAIL);
    let report = run_with_z3(&program, se_integration::common::cfg(64, 16));
    let mut ev = report.evidence[0].clone();
    // Mutate the witness above the width range; no silent masking allowed.
    ev.inputs.insert("x".to_string(), 10_000);
    let v = verify_evidence(&program, &ev, RunOpts::default());
    assert_eq!(v.status, "rejected");
    assert_eq!(v.replay_outcome, "invalid_input");
    assert!(v
        .reason
        .unwrap()
        .contains("exceeds 8-bit range"));
}

#[test]
fn witness_for_another_failure_site_is_rejected() {
    let program = parse(DIV_BY_ZERO);
    let report = run_with_z3(&program, se_integration::common::cfg(64, 16));
    let mut ev = report.evidence[0].clone();
    assert_eq!(ev.failure, se_lang::interp::FailureKind::DivByZero);
    // Claim an assertion failure at a non-existent later statement.
    ev.failure = se_lang::interp::FailureKind::Assertion;
    ev.stmt_id = 99;
    let v = verify_evidence(&program, &ev, RunOpts::default());
    assert_eq!(v.status, "rejected");
    assert!(v.replay_outcome == "div_by_zero");
    assert_eq!(v.replay_stmt, Some(1));
}

#[test]
fn in_width_but_out_of_declared_domain_is_rejected() {
    // Restricted domain [0,10]; value 11 is a valid u8 but outside the domain.
    let src = r#"{
      "width": 8,
      "inputs": [{"name": "x", "low": 0, "high": 10}],
      "body": [
        {"stmt": "assert",
         "cond": {"expr": "eq", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 0}}}
      ]
    }"#;
    let program = parse(src);
    let report = run_with_z3(&program, se_integration::common::cfg(64, 16));
    let mut ev = report.evidence[0].clone();
    ev.inputs.insert("x".to_string(), 11);
    let v = verify_evidence(&program, &ev, RunOpts::default());
    assert_eq!(v.status, "rejected");
    assert!(v
        .reason
        .unwrap()
        .contains("outside declared domain [0,10]"));
}

#[test]
fn undeclared_model_name_is_rejected() {
    let program = parse(ASSERT_FAIL);
    let report = run_with_z3(&program, se_integration::common::cfg(64, 16));
    let mut ev = report.evidence[0].clone();
    ev.inputs.insert("ghost".to_string(), 1);
    let v = verify_evidence(&program, &ev, RunOpts::default());
    assert_eq!(v.status, "rejected");
    assert!(v.reason.unwrap().contains("undeclared input"));
}

#[test]
fn normalize_masks_and_defaults() {
    let program = parse(ASSERT_FAIL);
    // Missing input gets its declared low bound.
    let empty = BTreeMap::new();
    let n = normalize_inputs(&program, &empty).unwrap();
    assert_eq!(n["x"], 0);

    // In-range values pass through masked.
    let mut good = BTreeMap::new();
    good.insert("x".to_string(), 42u64);
    assert_eq!(normalize_inputs(&program, &good).unwrap()["x"], 42);
}

#[test]
fn genuine_witness_confirms_with_reason_none() {
    let program = parse(ASSERT_FAIL);
    let report = run_with_z3(&program, se_integration::common::cfg(64, 16));
    let v = verify_evidence(&program, &report.evidence[0], RunOpts::default());
    assert_eq!(v.status, "confirmed");
    assert!(v.reason.is_none());
    assert!(v.replay_steps >= 1);
}
