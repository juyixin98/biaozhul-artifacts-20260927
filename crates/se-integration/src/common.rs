//! Shared program fixtures and run helpers for the independent test suite.
//!
//! Fixtures are hand-written JSON programs covering each required phenomenon. They
//! deliberately avoid generating expected answers through the code under test.

use se_engine::{Engine, EngineConfig};
use se_lang::dto::ProgramDto;
use se_lang::Program;
use se_solver::Z3Cli;

pub fn parse(src: &str) -> Program {
    ProgramDto::parse(src)
        .unwrap_or_else(|e| panic!("fixture must parse: {e}\n{src}"))
        .0
}

pub fn cfg(max_paths: usize, unroll: u32) -> EngineConfig {
    EngineConfig {
        max_paths,
        max_loop_unroll: unroll,
        enforce_domains: true,
        record_limit: 1000,
    }
}

pub fn run_with_z3(program: &Program, c: EngineConfig) -> se_engine::AnalysisReport {
    let z3 = Z3Cli::new("z3", 5_000);
    Engine::new(program, &z3, c).analyze()
}

pub fn z3_available() -> bool {
    Z3Cli::new("z3", 1000).available()
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/// Always-failing assertion reachable for x > 10 (u8, full domain).
pub const ASSERT_FAIL: &str = r#"{
  "width": 8,
  "inputs": [{"name": "x", "low": 0, "high": 255}],
  "body": [
    {"stmt": "assert",
     "cond": {"expr": "ule", "lhs": {"expr": "var", "name": "x"},
              "rhs": {"expr": "int", "value": 10}}}
  ]
}"#;

/// Mutually exclusive branches, each assertion holds on its branch.
pub const MUTEX_HOLDS: &str = r#"{
  "width": 8,
  "inputs": [{"name": "x", "low": 0, "high": 20}],
  "body": [
    {"stmt": "if",
     "cond": {"expr": "ult", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 5}},
     "then": [
       {"stmt": "assert",
        "cond": {"expr": "ult", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 10}}}
     ],
     "else": [
       {"stmt": "assert",
        "cond": {"expr": "uge", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 5}}}
     ]}
  ]
}"#;

/// Mutually exclusive branches where the *else* branch assertion can fail (x in 5..9).
pub const MUTEX_ONE_FAILS: &str = r#"{
  "width": 8,
  "inputs": [{"name": "x", "low": 0, "high": 20}],
  "body": [
    {"stmt": "if",
     "cond": {"expr": "ult", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 5}},
     "then": [
       {"stmt": "assert",
        "cond": {"expr": "ult", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 10}}}
     ],
     "else": [
       {"stmt": "assert",
        "cond": {"expr": "uge", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 10}}}
     ]}
  ]
}"#;

/// u8 wrap-around: y = x + 100; assert y > 200.
pub const WRAP_AROUND: &str = r#"{
  "width": 8,
  "inputs": [{"name": "x", "low": 0, "high": 255}],
  "body": [
    {"stmt": "assign", "target": "y",
     "expr": {"expr": "add", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 100}}},
    {"stmt": "assert",
     "cond": {"expr": "ugt", "lhs": {"expr": "var", "name": "y"}, "rhs": {"expr": "int", "value": 200}}}
  ]
}"#;

/// Infeasible branch protected by an assume; assertion in dead branch never reached.
pub const INFEASIBLE_BRANCH: &str = r#"{
  "width": 8,
  "inputs": [{"name": "x", "low": 0, "high": 255}],
  "body": [
    {"stmt": "assume",
     "cond": {"expr": "ult", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 3}}},
    {"stmt": "if",
     "cond": {"expr": "ugt", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 10}},
     "then": [{"stmt": "assert", "cond": {"expr": "int", "value": 0}}],
     "else": []}
  ]
}"#;

/// Division by zero reachable when x == 3 (guard x - 3 == 0).
pub const DIV_BY_ZERO: &str = r#"{
  "width": 8,
  "inputs": [{"name": "x", "low": 0, "high": 10}],
  "body": [
    {"stmt": "assign", "target": "d",
     "expr": {"expr": "sub", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 3}}},
    {"stmt": "assign", "target": "q",
     "expr": {"expr": "udiv", "lhs": {"expr": "int", "value": 42}, "rhs": {"expr": "var", "name": "d"}}}
  ]
}"#;

/// Trap-mode u8 addition overflow reachable (x >= 128).
pub const TRAP_OVERFLOW: &str = r#"{
  "width": 8,
  "overflow": "trap",
  "inputs": [{"name": "x", "low": 0, "high": 255}],
  "body": [
    {"stmt": "assign", "target": "y",
     "expr": {"expr": "add", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 200}}}
  ]
}"#;

/// Guarded division: divisor is zero only on an infeasible branch (x == 1 & x == 2).
pub const DIV_ZERO_INFEASIBLE: &str = r#"{
  "width": 8,
  "inputs": [{"name": "x", "low": 0, "high": 10}],
  "body": [
    {"stmt": "if",
     "cond": {"expr": "and",
       "lhs": {"expr": "eq", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 1}},
       "rhs": {"expr": "eq", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 2}}},
     "then": [
       {"stmt": "assign", "target": "q",
        "expr": {"expr": "udiv", "lhs": {"expr": "int", "value": 1}, "rhs": {"expr": "int", "value": 0}}}
     ],
     "else": []}
  ]
}"#;

/// Counting loop: after the loop i == n must hold (true for small n).
pub fn loop_counter(n_high: u64) -> String {
    format!(
        r#"{{
  "width": 8,
  "inputs": [{{"name": "n", "low": 0, "high": {n_high}}}],
  "vars": [{{"name": "i", "value": 0}}],
  "body": [
    {{ "stmt": "while",
      "cond": {{"expr": "ult", "lhs": {{"expr": "var", "name": "i"}}, "rhs": {{"expr": "var", "name": "n"}}}},
      "body": [
        {{"stmt": "assign", "target": "i",
         "expr": {{"expr": "add", "lhs": {{"expr": "var", "name": "i"}}, "rhs": {{"expr": "int", "value": 1}}}}}}
      ]}},
    {{ "stmt": "assert",
      "cond": {{"expr": "eq", "lhs": {{"expr": "var", "name": "i"}}, "rhs": {{"expr": "var", "name": "n"}}}}}}
  ]
}}"#
    )
}

/// Loop whose iteration count exceeds a small unroll budget (n up to 30, cap 8).
pub fn loop_over_budget(n_high: u64) -> String {
    loop_counter(n_high)
}
