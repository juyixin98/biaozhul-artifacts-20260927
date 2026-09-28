//! 本地 CLI 示例：读取 DIMACS CNF 文件，跑完整“解析→规范化→求解→独立复核”管线。
//!
//! 用法：
//!   cargo run --example solve_file -- fixtures/unsat_pigeonhole_small.cnf
//!   cargo run --example solve_file -- --max-decisions 0 fixtures/sat_chain.cnf

use std::process::ExitCode;

use cnf_solver_backend::cnf::normalize_formula;
use cnf_solver_backend::input::parse_dimacs;
use cnf_solver_backend::solver::{SolveLimits, Solver, Status};
use cnf_solver_backend::verify::{check_model, check_proof};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let mut path: Option<&str> = None;
    let mut max_decisions: Option<u64> = None;
    let mut time_limit_ms: Option<u64> = None;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--max-decisions" => {
                i += 1;
                max_decisions = args.get(i).and_then(|v| v.parse().ok());
            }
            "--time-limit-ms" => {
                i += 1;
                time_limit_ms = args.get(i).and_then(|v| v.parse().ok());
            }
            other if !other.starts_with("--") => path = Some(other),
            other => {
                eprintln!("unknown argument: {other}");
                return ExitCode::from(2);
            }
        }
        i += 1;
    }

    let Some(path) = path else {
        eprintln!("usage: solve_file [--max-decisions N] [--time-limit-ms N] <file.cnf>");
        return ExitCode::from(2);
    };

    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("cannot read {path}: {e}");
            return ExitCode::from(2);
        }
    };

    let parsed = match parse_dimacs(&text) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("parse error: {e}");
            return ExitCode::from(2);
        }
    };
    let (formula, notes) = match normalize_formula(parsed.declared_vars, &parsed.clauses) {
        Ok(x) => x,
        Err(e) => {
            eprintln!("normalization error: {e}");
            return ExitCode::from(2);
        }
    };
    if !notes.is_empty() {
        println!("normalization: {notes:?}");
    }

    let outcome = Solver::new(&formula).solve(&SolveLimits {
        max_decisions,
        time_limit: time_limit_ms.map(std::time::Duration::from_millis),
    });
    println!(
        "diagnostics: {}",
        serde_json::to_string(&outcome.diagnostics).unwrap()
    );

    match outcome.status {
        Status::Sat => {
            let model = outcome.model.unwrap();
            match check_model(&formula, &model) {
                Ok(()) => {
                    let dimacs: Vec<String> = (1..model.len())
                        .map(|v| {
                            if model[v] {
                                v.to_string()
                            } else {
                                format!("-{v}")
                            }
                        })
                        .collect();
                    println!(
                        "SAT model (independent checker ACCEPTED): {}",
                        dimacs.join(" ")
                    );
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("INTERNAL: model rejected by independent checker: {e:?}");
                    ExitCode::from(3)
                }
            }
        }
        Status::Unsat => {
            let proof = outcome.proof.unwrap();
            match check_proof(&formula, &proof) {
                Ok(()) => {
                    println!(
                        "UNSAT proof (independent checker ACCEPTED): {} derived clauses, final ref {}",
                        proof.derived_clauses.len(),
                        proof.empty_clause_ref
                    );
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("INTERNAL: proof rejected by independent checker: {e:?}");
                    ExitCode::from(3)
                }
            }
        }
        Status::Unknown => {
            println!(
                "UNKNOWN: {:?} — budget exhausted, NOT a refutation",
                outcome.diagnostics.stop_reason
            );
            ExitCode::from(4)
        }
    }
}
