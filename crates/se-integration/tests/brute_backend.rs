//! Engine against the independent brute-force solver (no Z3 involved).
//!
//! The brute solver decides feasibility by enumerating declared domains with its own
//! SMT-LIB parser/evaluator. Engine verdicts must still match the concrete oracle.

use std::collections::BTreeMap;

use se_engine::{Engine, EngineConfig};
use se_integration::brute::solver_for;
use se_integration::common::{
    parse, ASSERT_FAIL, DIV_BY_ZERO, INFEASIBLE_BRANCH, MUTEX_HOLDS, WRAP_AROUND,
};
use se_lang::interp::{FailureKind, RunOpts};
use se_verify::oracle::exhaustive_oracle;
use se_verify::verify_report;

fn engine_cfg() -> EngineConfig {
    EngineConfig {
        max_paths: 128,
        max_loop_unroll: 16,
        enforce_domains: true,
        record_limit: 1000,
    }
}

#[test]
fn brute_backend_drives_assertion_scenario() {
    let program = parse(ASSERT_FAIL);
    let brute = solver_for(&program, 4096);
    let report = Engine::new(&program, &brute, engine_cfg()).analyze();
    assert_eq!(report.verdict, "violation");
    let ev = &report.evidence[0];
    assert_eq!(ev.failure, FailureKind::Assertion);
    assert_eq!(ev.solver, "brute-indep");
    let x = ev.inputs["x"];
    assert!(x >= 11);
    let verified = verify_report(&program, &report, RunOpts::default());
    assert_eq!(verified.final_verdict, "violation");
    assert_eq!(verified.confirmed_count, 1);
}

#[test]
fn brute_backend_drives_wrap_and_divzero() {
    let program = parse(WRAP_AROUND);
    let brute = solver_for(&program, 4096);
    let report = Engine::new(&program, &brute, engine_cfg()).analyze();
    assert_eq!(report.verdict, "violation");
    let oracle = exhaustive_oracle(&program, 4096, RunOpts::default());
    let bad: std::collections::BTreeSet<u64> =
        oracle.failures.iter().map(|f| f.inputs["x"]).collect();
    assert!(bad.contains(&report.evidence[0].inputs["x"]));

    let program = parse(DIV_BY_ZERO);
    let brute = solver_for(&program, 4096);
    let report = Engine::new(&program, &brute, engine_cfg()).analyze();
    assert_eq!(report.evidence[0].failure, FailureKind::DivByZero);
    assert_eq!(report.evidence[0].inputs["x"], 3);
}

#[test]
fn brute_backend_holds_when_all_paths_safe() {
    let program = parse(MUTEX_HOLDS);
    let brute = solver_for(&program, 4096);
    let report = Engine::new(&program, &brute, engine_cfg()).analyze();
    assert_eq!(report.verdict, "holds");
    assert!(report.cuts.is_empty());
    assert_eq!(report.budget.explored_terminals, 2);

    let program = parse(INFEASIBLE_BRANCH);
    let brute = solver_for(&program, 4096);
    let report = Engine::new(&program, &brute, engine_cfg()).analyze();
    assert_eq!(report.verdict, "holds");
    assert!(report.evidence.is_empty());
}

#[test]
fn brute_solver_refuses_to_guess_when_domain_exceeds_cap() {
    // Full u8 domain is 256; cap of 16 forces unknown on feasibility queries.
    let program = parse(ASSERT_FAIL);
    let brute = solver_for(&program, 16);
    let report = Engine::new(&program, &brute, engine_cfg()).analyze();
    // With unknown feasibility the engine must not claim holds.
    assert_eq!(report.verdict, "unknown");
    assert!(report.cuts.iter().any(|c| {
        matches!(c.kind, se_engine::CutKind::SolverUnknown)
    }));
    // And it must not fabricate evidence from an unknown answer.
    assert!(report.evidence.is_empty());
    let _: BTreeMap<_, _> = BTreeMap::<u8, u8>::new();
}
