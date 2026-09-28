//! 命令行求解器：`cnf-cli [--time-ms N] [--max-propagations N] <file.cnf>`
//!
//! 主要用于本地烟雾测试与离线验证，不启动 HTTP 服务。

use std::fs;
use std::process::ExitCode;
use std::time::Duration;

use cnf_dpll::evidence::checker::check_outcome;
use cnf_dpll::input::parse_dimacs;
use cnf_dpll::normalize::normalize_signed_clauses;
use cnf_dpll::solver::{solve_normalized, Budget};
use cnf_dpll::evidence::types::Outcome;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let mut time_ms: u64 = 10_000;
    let mut max_prop: u64 = 10_000_000;
    let mut path: Option<String> = None;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--time-ms" => {
                i += 1;
                time_ms = args[i].parse().expect("--time-ms 需要整数");
            }
            "--max-propagations" => {
                i += 1;
                max_prop = args[i].parse().expect("--max-propagations 需要整数");
            }
            "-h" | "--help" => {
                println!("用法: cnf-cli [--time-ms N] [--max-propagations N] <file.cnf>");
                return ExitCode::SUCCESS;
            }
            other => path = Some(other.to_string()),
        }
        i += 1;
    }
    let path = match path {
        Some(p) => p,
        None => {
            eprintln!("缺少输入文件；用法: cnf-cli <file.cnf>");
            return ExitCode::from(2);
        }
    };

    let src = match fs::read_to_string(&path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("读取 {path} 失败: {e}");
            return ExitCode::from(2);
        }
    };
    let parsed = match parse_dimacs(&src) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("解析失败 [{e}]");
            return ExitCode::from(2);
        }
    };
    let raw = parsed.clauses.clone();
    let cnf = normalize_signed_clauses(parsed.num_vars, &parsed.clauses);
    let budget = Budget {
        time_limit: Some(Duration::from_millis(time_ms)),
        max_propagations: Some(max_prop),
        max_decisions: Some(1_000_000),
    };
    let result = solve_normalized(&cnf, &budget);

    println!("normalize: {:?}", result.normalize_report);
    println!(
        "counters: decisions={} propagations={} conflicts={} learned={}",
        result.counters.decisions,
        result.counters.propagations,
        result.counters.conflicts,
        result.counters.learned_clauses
    );
    match &result.outcome {
        Outcome::Sat { model } => {
            println!("SAT");
            println!(
                "model: {}",
                model
                    .true_literals
                    .iter()
                    .map(|l| cnf_dpll::lit::lit_to_signed(*l).to_string())
                    .collect::<Vec<_>>()
                    .join(" ")
            );
            if let Err(e) = check_outcome(parsed.num_vars, &raw, &result.outcome)
            {
                eprintln!("内部交叉验证失败: {e}");
                return ExitCode::FAILURE;
            }
            println!("checker: MODEL_VALID");
            ExitCode::SUCCESS
        }
        Outcome::Unsat { proof } => {
            println!("UNSAT ({} proof steps)", proof.steps.len());
            if let Err(e) = check_outcome(parsed.num_vars, &raw, &result.outcome)
            {
                eprintln!("内部交叉验证失败: {e}");
                return ExitCode::FAILURE;
            }
            println!("checker: PROOF_VALID");
            ExitCode::SUCCESS
        }
        Outcome::Unknown { reason, .. } => {
            println!("UNKNOWN: {reason}");
            // UNKNOWN 是可靠的"无法判定"，退出码区别于 0/1，供脚本区分。
            ExitCode::from(3)
        }
    }
}
