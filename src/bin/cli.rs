//! Command-line entry point.
//!
//! Subcommands:
//! - `serve [BIND_ADDR]`: run the Axum HTTP server (default 127.0.0.1:8080).
//! - `online <ruleset.json> <trace.json>`: drive the online kernel over a
//!   trace file.
//! - `offline <ruleset.json> <trace.json>`: run the independent unfold oracle.
//! - `verify <evidence.json>`: verify an evidence bundle.
//!
//! Trace file format: `{"steps": [ <step>, ... ]}`.  Exit code is non-zero
//! on input/computation errors and (for `online`) additionally when the
//! final aggregate verdict is `violated`.

use std::process::ExitCode;

use bounded_monitor::api::router;
use bounded_monitor::error::AppError;
use bounded_monitor::evidence::EvidenceBundle;
use bounded_monitor::kernel::{Limits, Monitor, Verdict};
use bounded_monitor::language::{Ruleset, Step};
use bounded_monitor::oracle;
use serde::Deserialize;

#[derive(Deserialize)]
struct TraceFile {
    steps: Vec<Step>,
}

#[tokio::main]
async fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    match run(&args).await {
        Ok(code) => code,
        Err(err) => {
            eprintln!("error: {err}");
            ExitCode::from(2)
        }
    }
}

async fn run(args: &[String]) -> Result<ExitCode, AppError> {
    let cmd = args.get(1).map(String::as_str).unwrap_or("help");
    match cmd {
        "serve" => {
            let addr = args.get(2).cloned().unwrap_or_else(|| "127.0.0.1:8080".to_string());
            serve(addr).await?;
            Ok(ExitCode::from(0))
        }
        "online" => {
            let (ruleset, trace) = load_inputs(args)?;
            let mut monitor = Monitor::new(ruleset, Limits::default())?;
            for step in &trace.steps {
                let outcome = monitor.apply_step(step)?;
                println!(
                    "step {:>4} spawned={:?} resolved={:?} violated={:?} verdict={}",
                    outcome.index,
                    outcome.spawned,
                    outcome.resolved,
                    outcome.violated,
                    outcome.verdict.as_str()
                );
            }
            let verdict = monitor.verdict();
            println!("final verdict: {}", verdict.as_str());
            Ok(if verdict == Verdict::Violated { ExitCode::from(1) } else { ExitCode::from(0) })
        }
        "offline" => {
            let (ruleset, trace) = load_inputs(args)?;
            let report = oracle::evaluate(&ruleset, &trace.steps)?;
            for o in &report.obligations {
                println!(
                    "{:20} kind={:<8} status={:<9} trigger={:?} deadline={:?} resolution={:?} violation={:?} reason={}",
                    o.id,
                    format!("{:?}", o.kind).to_lowercase(),
                    format!("{:?}", o.status).to_lowercase(),
                    o.trigger_step,
                    o.deadline_step,
                    o.resolution_step,
                    o.violation_step,
                    o.reason
                );
            }
            println!(
                "closed={} final verdict: {}",
                report.closed,
                report.verdict.as_str()
            );
            Ok(if report.verdict == Verdict::Violated {
                ExitCode::from(1)
            } else {
                ExitCode::from(0)
            })
        }
        "verify" => {
            let path = args.get(2).ok_or_else(|| usage("verify <evidence.json>"))?;
            let bytes = std::fs::read(path)
                .map_err(|e| AppError::input("cannot_read_file", format!("{path}: {e}")))?;
            let bundle: EvidenceBundle = serde_json::from_slice(&bytes)
                .map_err(|e| AppError::input("malformed_json", format!("{path}: {e}")))?;
            let report = bundle.verify()?;
            println!("{}", serde_json::to_string_pretty(&report).unwrap());
            Ok(if report.valid { ExitCode::from(0) } else { ExitCode::from(1) })
        }
        "help" | "-h" | "--help" => {
            print_usage();
            Ok(ExitCode::from(0))
        }
        other => {
            print_usage();
            Err(AppError::input("unknown_command", format!("`{other}`")))
        }
    }
}

fn load_inputs(args: &[String]) -> Result<(Ruleset, TraceFile), AppError> {
    let rs_path = args.get(2).ok_or_else(|| usage("<ruleset.json> <trace.json>"))?;
    let tr_path = args.get(3).ok_or_else(|| usage("<ruleset.json> <trace.json>"))?;
    let ruleset: Ruleset = read_json(rs_path)?;
    let trace: TraceFile = read_json(tr_path)?;
    Ok((ruleset, trace))
}

fn read_json<T: for<'de> Deserialize<'de>>(path: &str) -> Result<T, AppError> {
    let bytes =
        std::fs::read(path).map_err(|e| AppError::input("cannot_read_file", format!("{path}: {e}")))?;
    serde_json::from_slice(&bytes)
        .map_err(|e| AppError::input("malformed_json", format!("{path}: {e}")))
}

fn usage(msg: &'static str) -> AppError {
    AppError::input("bad_usage", msg)
}

fn print_usage() {
    eprintln!(
        "bounded-monitor\n\
         \n\
         USAGE:\n  \
         bounded-monitor serve [BIND_ADDR]\n  \
         bounded-monitor online  <ruleset.json> <trace.json>\n  \
         bounded-monitor offline <ruleset.json> <trace.json>\n  \
         bounded-monitor verify  <evidence.json>\n"
    );
}

async fn serve(addr: String) -> Result<(), AppError> {
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .map_err(|e| AppError::input("bind_failed", format!("{addr}: {e}")))?;
    eprintln!("bounded-monitor listening on http://{addr}");
    let store = std::sync::Arc::new(bounded_monitor::store::Store::new());
    axum::serve(listener, router(store))
        .with_graceful_shutdown(shutdown_signal())
        .await
        .map_err(|e| AppError::compute("server_error", e.to_string()))
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    eprintln!("shutting down");
}
