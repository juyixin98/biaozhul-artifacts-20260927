//! Shared helpers for integration tests.
//!
//! The oracle used here is the *concrete* interpreter in `evidence`, which is
//! implemented independently of the SMT kernel. Expected values in the tests
//! themselves are hand-written literals, so no assertion derives its answer
//! from the component under test.

use std::collections::HashMap;

use symex::config::EngineConfig;
use symex::evidence::concrete::{run as run_concrete, ConcreteInput, ConcreteOutcome};
use symex::kernel::analyze;
use symex::kernel::report::AnalysisReport;
use symex::lang::{program_from_source, Program};

/// Parse + validate a program, panicking with a readable message on failure.
pub fn prog(src: &str) -> Program {
    program_from_source(src).unwrap_or_else(|e| panic!("program should parse: {e}\n---\n{src}"))
}

/// Run the kernel with default-small budgets.
pub fn analyze_src(src: &str) -> AnalysisReport {
    analyze_src_cfg(src, &test_cfg())
}

pub fn analyze_src_cfg(src: &str, cfg: &EngineConfig) -> AnalysisReport {
    let p = prog(src);
    analyze(p, cfg.clone(), "test-run".into()).expect("analysis should not hit replay mismatch")
}

pub fn test_cfg() -> EngineConfig {
    EngineConfig {
        max_paths: 512,
        loop_unroll: 32,
        solver_timeout_ms: 5_000,
    }
}

/// Exhaustively run the concrete interpreter over the full parameter domain
/// (only call this for small widths) and return the inputs that fail, with
/// their failure site.
pub fn exhaustive_failures(
    p: &Program,
) -> Vec<(ConcreteInput, symex::evidence::concrete::FailureSite)> {
    let sizes: Vec<u64> = p.params.iter().map(|pr| 1u64 << pr.ty.bits()).collect();
    let total: u128 = sizes.iter().map(|s| *s as u128).product();
    assert!(total <= 1 << 24, "domain too large for exhaustive test");
    let mut out = Vec::new();
    for mixed in 0..total as u64 {
        let mut rest = mixed;
        let mut input: ConcreteInput = HashMap::new();
        for pr in &p.params {
            let v = rest & pr.ty.mask();
            rest /= 1u64 << pr.ty.bits();
            input.insert(pr.name.clone(), v);
        }
        let r = run_concrete(p, &input).expect("concrete run");
        if r.outcome == ConcreteOutcome::Failed {
            out.push((input, r.failure.expect("failed run has a site")));
        }
    }
    out
}
