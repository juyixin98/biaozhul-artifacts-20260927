//! Additional engine-vs-oracle agreement across several small programs and widths.

use se_engine::Engine;
use se_integration::brute::solver_for;
use se_integration::common::{parse, cfg, run_with_z3};
use se_lang::interp::RunOpts;
use se_verify::oracle::exhaustive_oracle;

/// Run both solvers' analyses and the exhaustive oracle, compare verdicts.
fn assert_all_agree(src: &str, cap: u64) {
    let program = parse(src);
    let oracle = exhaustive_oracle(&program, cap, RunOpts::default());
    if oracle.truncated {
        panic!("oracle truncated; pick a smaller domain for {src}");
    }

    let zreport = run_with_z3(&program, cfg(256, 32));
    assert_eq!(zreport.verdict, oracle.verdict, "z3 verdict mismatch for {src}");

    let brute = solver_for(&program, cap);
    let breport = Engine::new(&program, &brute, cfg(256, 32)).analyze();
    assert_eq!(
        breport.verdict, oracle.verdict,
        "brute verdict mismatch for {src}"
    );

    // Number of distinct failing sites must match when violations exist.
    if oracle.verdict == "violation" {
        let sites: std::collections::BTreeSet<(String, usize)> =
            oracle.failure_sites.iter().cloned().collect();
        let engine_sites: std::collections::BTreeSet<(String, usize)> = zreport
            .evidence
            .iter()
            .map(|e| (e.failure.as_str().to_string(), e.stmt_id))
            .collect();
        assert_eq!(engine_sites, sites, "failing sites mismatch for {src}");
    }
}

#[test]
fn width16_signed_comparison_program() {
    let src = r#"{
      "width": 16,
      "inputs": [{"name": "x", "low": 65530, "high": 65535}],
      "body": [
        {"stmt": "assert",
         "cond": {"expr": "slt", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 0}}}
      ]
    }"#;
    // 65530..65535 are -6..-1 signed => all pass.
    assert_all_agree(src, 64);
}

#[test]
fn ite_branch_guard_does_not_fire_on_unselected_divisor() {
    // y = x != 0 ? 10/x : 7. No div-by-zero because the division branch is guarded.
    let src = r#"{
      "width": 8,
      "inputs": [{"name": "x", "low": 0, "high": 5}],
      "body": [
        {"stmt": "assign", "target": "y",
         "expr": {"expr": "ite",
           "cond": {"expr": "ne", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 0}},
           "then": {"expr": "udiv", "lhs": {"expr": "int", "value": 10}, "rhs": {"expr": "var", "name": "x"}},
           "else": {"expr": "int", "value": 7}}}
      ]
    }"#;
    assert_all_agree(src, 64);
}

#[test]
fn nested_branch_combinations_agree() {
    let src = r#"{
      "width": 8,
      "inputs": [{"name": "a", "low": 0, "high": 1}, {"name": "b", "low": 0, "high": 1}],
      "body": [
        {"stmt": "if", "cond": {"expr": "eq", "lhs": {"expr": "var", "name": "a"}, "rhs": {"expr": "int", "value": 1}},
         "then": [
           {"stmt": "if", "cond": {"expr": "eq", "lhs": {"expr": "var", "name": "b"}, "rhs": {"expr": "int", "value": 1}},
            "then": [{"stmt": "assert", "cond": {"expr": "int", "value": 1}}],
            "else": [{"stmt": "assert", "cond": {"expr": "int", "value": 0}}]}
         ],
         "else": [{"stmt": "assert", "cond": {"expr": "int", "value": 1}}]}
      ]
    }"#;
    // Exactly one of 4 assignments fails (a=1,b=0).
    assert_all_agree(src, 64);
    let program = parse(src);
    let oracle = exhaustive_oracle(&program, 64, RunOpts::default());
    assert_eq!(oracle.failures.len(), 1);
    assert_eq!(oracle.failures[0].inputs["a"], 1);
    assert_eq!(oracle.failures[0].inputs["b"], 0);
}

#[test]
fn shifts_and_bitwise_agree() {
    let src = r#"{
      "width": 8,
      "inputs": [{"name": "x", "low": 0, "high": 7}, {"name": "s", "low": 0, "high": 9}],
      "body": [
        {"stmt": "assign", "target": "a",
         "expr": {"expr": "shl", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "var", "name": "s"}}},
        {"stmt": "assign", "target": "b",
         "expr": {"expr": "xor", "lhs": {"expr": "var", "name": "a"}, "rhs": {"expr": "int", "value": 255}}},
        {"stmt": "assert",
         "cond": {"expr": "eq", "lhs": {"expr": "var", "name": "b"}, "rhs": {"expr": "int", "value": 0}}}
      ]
    }"#;
    assert_all_agree(src, 256);
}
