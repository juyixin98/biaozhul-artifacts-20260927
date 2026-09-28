//! CLI and server entry point.
//!
//! Subcommands:
//! - `interval-analyzer serve [--config PATH] [--addr 127.0.0.1:8080]`
//! - `interval-analyzer analyze <program.isl> [--config PATH]` (JSON to stdout)
//! - `interval-analyzer verify <program.isl> <report.json>`

use interval_analyzer::api::{self, AppState};
use interval_analyzer::config::Config;
use interval_analyzer::evidence;
use interval_analyzer::lang::parse;
use interval_analyzer::{analyze_source, report::AnalysisReport};
use std::fs;
use std::process::ExitCode;
use tracing::{error, info};
use tracing_subscriber::{fmt, EnvFilter};

fn main() -> ExitCode {
    init_tracing();
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("serve") => serve(&args[2..]),
        Some("analyze") => analyze_cmd(&args[2..]),
        Some("verify") => verify_cmd(&args[2..]),
        other => {
            eprintln!("usage: interval-analyzer <serve|analyze|verify> ...");
            if let Some(o) = other {
                eprintln!("unknown subcommand: {o}");
            }
            ExitCode::from(2)
        }
    }
}

fn init_tracing() {
    let filter =
        EnvFilter::try_from_env("INTERVAL_ANALYZER_LOG").unwrap_or_else(|_| EnvFilter::new("info"));
    fmt().with_env_filter(filter).with_target(false).init();
}

fn load_config(args: &[String]) -> (Config, Vec<String>) {
    let mut cfg = Config::default();
    let mut rest = Vec::new();
    let mut i = 0;
    while i < args.len() {
        if args[i] == "--config" && i + 1 < args.len() {
            let path = &args[i + 1];
            let text = fs::read_to_string(path).unwrap_or_else(|e| {
                error!("cannot read config {path}: {e}");
                std::process::exit(2);
            });
            cfg = Config::load_toml(&text).unwrap_or_else(|e| {
                error!("invalid config {path}: {e}");
                std::process::exit(2);
            });
            i += 2;
        } else {
            rest.push(args[i].clone());
            i += 1;
        }
    }
    (cfg, rest)
}

fn analyze_cmd(args: &[String]) -> ExitCode {
    let (cfg, rest) = load_config(args);
    let Some(path) = rest.first() else {
        eprintln!("usage: interval-analyzer analyze <program.isl> [--config PATH]");
        return ExitCode::from(2);
    };
    let source = match fs::read_to_string(path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("cannot read {path}: {e}");
            return ExitCode::from(2);
        }
    };
    match analyze_source(&source, cfg) {
        Ok(report) => match serde_json::to_string_pretty(&report) {
            Ok(json) => {
                println!("{json}");
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("failed to serialise report: {e}");
                ExitCode::FAILURE
            }
        },
        Err(e) => {
            eprintln!("{e}");
            ExitCode::from(1)
        }
    }
}

fn verify_cmd(args: &[String]) -> ExitCode {
    let Some(source_path) = args.first() else {
        eprintln!("usage: interval-analyzer verify <program.isl> <report.json>");
        return ExitCode::from(2);
    };
    let Some(report_path) = args.get(1) else {
        eprintln!("usage: interval-analyzer verify <program.isl> <report.json>");
        return ExitCode::from(2);
    };
    let source = match fs::read_to_string(source_path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("cannot read {source_path}: {e}");
            return ExitCode::from(2);
        }
    };
    let report_text = match fs::read_to_string(report_path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("cannot read {report_path}: {e}");
            return ExitCode::from(2);
        }
    };
    let report: AnalysisReport = match serde_json::from_str(&report_text) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("invalid report JSON: {e}");
            return ExitCode::from(1);
        }
    };
    let program = match parse(&source) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(1);
        }
    };
    let result = evidence::verify(&source, &program, &report);
    println!(
        "{}",
        serde_json::to_string_pretty(&result).unwrap_or_default()
    );
    if result.ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

fn serve(args: &[String]) -> ExitCode {
    let (cfg, rest) = load_config(args);
    let addr = match rest.first() {
        Some(a) => a.clone(),
        None => "127.0.0.1:8080".to_string(),
    };
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            error!("failed to start tokio runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    runtime.block_on(async move {
        let listener = match tokio::net::TcpListener::bind(&addr).await {
            Ok(l) => l,
            Err(e) => {
                error!("failed to bind {addr}: {e}");
                return ExitCode::FAILURE;
            }
        };
        info!(%addr, "interval analyzer listening");
        let app = api::router(AppState {
            default_config: cfg,
        });
        if let Err(e) = axum::serve(listener, app).await {
            error!("server error: {e}");
            return ExitCode::FAILURE;
        }
        ExitCode::SUCCESS
    })
}
