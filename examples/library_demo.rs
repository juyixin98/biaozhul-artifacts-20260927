//! Library usage example (no HTTP): parse a formula, extract one core, and let the
//! independent oracle certify it.
//!
//! Run with: `cargo run --offline --example library_demo`

use mus_core::extract::{extract, ExtractionOptions};
use mus_core::language::parse_cnf;
use mus_core::solver::{builtin::DpllSolver, oracle::BruteForceSolver, CancelToken};
use mus_core::verify::verify_report;

fn main() {
    // Two disjoint conflicts {u,v} and {p,q}, plus a tautology r1.
    let input = "\
4
u: 1 0
v: -1 0
p: 2 0
q: -2 0
r1: 4 -4 0
";
    let cnf = parse_cnf(input).expect("parse fixture");

    let primary = DpllSolver::default();
    let cancel = CancelToken::new();
    let report = extract(&cnf, &primary, &ExtractionOptions::default(), &cancel);

    println!("termination: {:?}", report.termination);
    for (i, core) in report.cores.iter().enumerate() {
        println!("core {i}: {:?} ({:?})", core.member_ids, core.verdict);
    }
    for t in &report.trace {
        println!(
            "  seq={} {:?} tested={:?} verdict={} kept={:?}",
            t.seq, t.phase, t.tested_id, t.verdict, t.kept
        );
    }

    // Independent certification by the brute-force truth-table backend.
    let oracle = BruteForceSolver::new();
    let verification = verify_report(&cnf, &report, &oracle, 0, report.trace.len() as u64 + 64);
    println!(
        "independent verification: all_certified={} (solver={})",
        verification.all_certified, verification.independent_solver
    );
    assert!(verification.all_certified);
}
