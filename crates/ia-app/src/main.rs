//! `ia` command line:
//!
//! ```text
//! ia serve --bind 127.0.0.1:8080
//! ia analyze [--config <json>] [--json] [--no-narrowing] [--plain]
//!            [--max-trace N] [--request-id ID] <file.ial>
//! ia verify  [--config <json>] [--json] [--cap N] [--steps N]
//!            [--request-id ID] <file.ial>
//! ```
use axum::serve;
use ia_app::config::{analyzer_config, verify_config};
use ia_app::{run_analyze_with, run_verify_with};
use ia_solver::CheckVerdict;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::process::ExitCode;
use tokio::net::TcpListener;

fn usage() -> ! {
    eprintln!(
        "usage:\n  \
         ia serve --bind <addr:port>\n  \
         ia analyze [--config <json>] [--json] [--no-narrowing] [--plain]\n  \
         {0:38}[--max-trace N] [--request-id ID] <file.ial>\n  \
         ia verify  [--config <json>] [--json] [--cap N] [--steps N]\n  \
         {0:38}[--request-id ID] <file.ial>",
        ""
    );
    std::process::exit(2);
}

fn read_source(path: &str) -> String {
    match std::fs::read_to_string(path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("cannot read {path}: {e}");
            std::process::exit(2);
        }
    }
}

fn print_envelope_diagnostics<T: serde::Serialize>(env: &ia_app::dto::Envelope<T>) -> bool {
    if env.ok {
        return true;
    }
    eprintln!("request {} failed:", env.request_id);
    for d in &env.diagnostics {
        eprintln!(
            "  line {} col {}: {}\n{}",
            d.start_line, d.start_column, d.message, d.source_excerpt
        );
    }
    false
}

fn cmd_analyze(args: &[String]) -> ExitCode {
    let mut json = false;
    let mut cli_narrowing: Option<bool> = None;
    let mut cli_plain: Option<bool> = None;
    let mut cli_trace: Option<usize> = None;
    let mut config_path: Option<PathBuf> = None;
    let mut request_id: Option<String> = None;
    let mut file: Option<String> = None;

    let mut i = 0;
    while i < args.len() {
        let a = args[i].clone();
        i += 1;
        let take_value = |i: &mut usize, name: &str| -> String {
            match args.get(*i) {
                Some(v) => {
                    *i += 1;
                    v.clone()
                }
                None => {
                    eprintln!("missing value for {name}");
                    usage()
                }
            }
        };
        match a.as_str() {
            "--json" => json = true,
            "--no-narrowing" => cli_narrowing = Some(false),
            "--narrowing" => cli_narrowing = Some(true),
            "--plain" => cli_plain = Some(true),
            "--max-trace" => {
                let v = take_value(&mut i, "--max-trace");
                cli_trace = Some(v.parse().unwrap_or_else(|_| {
                    eprintln!("--max-trace requires a non-negative integer");
                    usage()
                }));
            }
            "--config" => config_path = Some(PathBuf::from(take_value(&mut i, "--config"))),
            "--request-id" => request_id = Some(take_value(&mut i, "--request-id")),
            other if !other.starts_with('-') && file.is_none() => file = Some(a),
            _ => usage(),
        }
    }
    let Some(file) = file else { usage() };
    let cfg = match analyzer_config(
        config_path.as_deref(),
        cli_narrowing,
        cli_plain,
        cli_trace,
    ) {
        Ok(cfg) => cfg,
        Err(e) => {
            eprintln!("{}", e.message);
            return ExitCode::from(2);
        }
    };
    let source = read_source(&file);
    let env = run_analyze_with(source, request_id, cfg);
    if !print_envelope_diagnostics(&env) {
        return ExitCode::from(1);
    }
    let report = env.data.as_ref().unwrap();
    if json {
        println!("{}", serde_json::to_string_pretty(&env).unwrap());
        return ExitCode::SUCCESS;
    }
    println!("request {}  solver {}", env.request_id, env.solver_version);
    for step in &env.steps {
        println!("  [{:<8}] {}", step.stage, step.detail);
    }
    println!();
    for c in &report.checks {
        let tag = match c.verdict {
            CheckVerdict::Safe => "SAFE       ",
            CheckVerdict::PossibleFailure => "POSSIBLE   ",
            CheckVerdict::GuaranteedFailure => "GUARANTEED ",
            CheckVerdict::Unreachable => "UNREACHABLE",
        };
        println!(
            "  {tag} {:<13} at line {:>3} col {:>3}  {}",
            c.kind.as_str(),
            c.span.start.line,
            c.span.start.column,
            c.explanation
        );
    }
    println!(
        "\n  totals: {} safe, {} possible (over-approximation, not proven bugs), {} guaranteed, {} unreachable",
        report.counts.safe,
        report.counts.possible_failure,
        report.counts.guaranteed_failure,
        report.counts.unreachable
    );
    ExitCode::SUCCESS
}

fn cmd_verify(args: &[String]) -> ExitCode {
    let mut json = false;
    let mut cap: Option<u64> = None;
    let mut steps: Option<u64> = None;
    let mut narrowing: Option<bool> = None;
    let mut config_path: Option<PathBuf> = None;
    let mut request_id: Option<String> = None;
    let mut file: Option<String> = None;

    let mut i = 0;
    while i < args.len() {
        let a = args[i].clone();
        i += 1;
        match a.as_str() {
            "--json" => json = true,
            "--cap" => {
                cap = args
                    .get(i)
                    .and_then(|s| s.parse().ok())
                    .or_else(|| usage());
                i += 1;
            }
            "--steps" => {
                steps = args
                    .get(i)
                    .and_then(|s| s.parse().ok())
                    .or_else(|| usage());
                i += 1;
            }
            "--no-narrowing" => narrowing = Some(false),
            "--narrowing" => narrowing = Some(true),
            "--config" => {
                config_path = Some(PathBuf::from(args.get(i).cloned().unwrap_or_else(|| usage())));
                i += 1;
            }
            "--request-id" => {
                request_id = Some(args.get(i).cloned().unwrap_or_else(|| usage()));
                i += 1;
            }
            other if !other.starts_with('-') && file.is_none() => file = Some(a),
            _ => usage(),
        }
    }
    let Some(file) = file else { usage() };
    let cfg = match verify_config(config_path.as_deref(), cap, steps, narrowing) {
        Ok(cfg) => cfg,
        Err(e) => {
            eprintln!("{}", e.message);
            return ExitCode::from(2);
        }
    };
    let source = read_source(&file);
    let env = run_verify_with(source, request_id, cfg);
    if !print_envelope_diagnostics(&env) {
        // Not-executed checks are diagnostics, exit non-zero but distinct
        // message already printed.
        return ExitCode::from(3);
    }
    let report = env.data.as_ref().unwrap();
    if json {
        println!("{}", serde_json::to_string_pretty(&env).unwrap());
        return if report.sound {
            ExitCode::SUCCESS
        } else {
            ExitCode::from(4)
        };
    }
    println!("request {}", env.request_id);
    for step in &env.steps {
        println!("  [{:<8}] {}", step.stage, step.detail);
    }
    println!(
        "  domain coverage: {}/{} ({})",
        report.combinations_run,
        report.combinations_declared,
        if report.enumeration_complete { "exhaustive" } else { "partial" }
    );
    println!(
        "  runs: {} normal, {} failed (overflow={}, div0={}, oob={}, assert={}, step_limit={})",
        report.normal_runs,
        report.failed_runs,
        report.failure_bucket.overflow,
        report.failure_bucket.div_by_zero,
        report.failure_bucket.index_out_of_bounds,
        report.failure_bucket.assertion_failed,
        report.failure_bucket.step_limit
    );
    for v in &report.violations {
        println!("  VIOLATION {:?}: {}", v.kind, v.detail);
    }
    if !report.not_cross_validated.is_empty() {
        println!(
            "  not cross-validated due to partial domain coverage: {}",
            report.not_cross_validated.join(", ")
        );
    }
    if report.sound {
        println!("  RESULT: sound — every concrete result contained in abstract result");
        ExitCode::SUCCESS
    } else {
        println!("  RESULT: UNSOUND — see violations above");
        ExitCode::from(4)
    }
}

async fn cmd_serve(args: &[String]) -> ExitCode {
    let mut bind: SocketAddr = "127.0.0.1:8080".parse().unwrap();
    let mut i = 0;
    while i < args.len() {
        if args[i] == "--bind" {
            match args.get(i + 1) {
                Some(v) => match v.parse() {
                    Ok(addr) => bind = addr,
                    Err(e) => {
                        eprintln!("bad --bind address: {e}");
                        return ExitCode::from(2);
                    }
                },
                None => usage(),
            }
            i += 2;
        } else {
            usage()
        }
    }
    let listener = match TcpListener::bind(bind).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("cannot bind {bind}: {e}");
            return ExitCode::from(2);
        }
    };
    eprintln!("interval-analysis-service listening on http://{bind}");
    if let Err(e) = serve(listener, ia_app::http::router()).await {
        eprintln!("server error: {e}");
        return ExitCode::from(1);
    }
    ExitCode::SUCCESS
}

#[tokio::main]
async fn main() -> ExitCode {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("analyze") => cmd_analyze(&args.collect::<Vec<_>>()),
        Some("verify") => cmd_verify(&args.collect::<Vec<_>>()),
        Some("serve") => cmd_serve(&args.collect::<Vec<_>>()).await,
        _ => usage(),
    }
}
