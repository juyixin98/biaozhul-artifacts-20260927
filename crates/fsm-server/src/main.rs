//! CLI / server entry point.
//!
//! Subcommands:
//! * `serve`  — run the Axum HTTP API (default)
//! * `check`  — one-shot: read a spec JSON file, run the kernel, print the
//!   full JSON result (same payload shape as POST /check) to stdout
//!
//! Exit codes: `0` complete run with no error; `2` invalid specification;
//! `3` evaluation error during exploration; `4` truncated; `1` other I/O
//! failure.

use clap::{Parser, Subcommand};
use fsm_core::{explore, Budget, RunStatus};
use fsm_evidence::replay;
use fsm_lang::fingerprint;
use fsm_lang::{CompiledSpec, Spec};
use fsm_server::app;
use fsm_server::config;
use serde_json::json;

#[derive(Parser)]
#[command(name = "fsm-server", version, about = "Local explicit-state FSM model checker")]
struct Cli {
    /// Path to an optional TOML config file.
    #[arg(long, global = true)]
    config: Option<String>,

    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Start the HTTP API.
    Serve {
        #[arg(long)]
        host: Option<String>,
        #[arg(long)]
        port: Option<u16>,
    },
    /// Check one specification file and print JSON to stdout.
    Check {
        /// Path to a JSON file containing either a bare Spec or a
        /// CheckRequest (`{"spec": ..., "budget": ...}`).
        file: String,
        #[arg(long)]
        max_states: Option<u64>,
        #[arg(long)]
        max_transitions: Option<u64>,
        #[arg(long)]
        max_initial_scan: Option<u64>,
    },
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .with_target(false)
        .init();

    let cli = Cli::parse();
    let result = match cli.command.unwrap_or(Command::Serve {
        host: None,
        port: None,
    }) {
        Command::Serve { host, port } => serve(cli.config, host, port).await,
        Command::Check {
            file,
            max_states,
            max_transitions,
            max_initial_scan,
        } => check_file(
            cli.config,
            &file,
            max_states,
            max_transitions,
            max_initial_scan,
        ),
    };

    match result {
        Ok(code) => std::process::exit(code),
        Err(e) => {
            eprintln!("error: {e}");
            std::process::exit(1);
        }
    }
}

async fn serve(
    config_path: Option<String>,
    host_override: Option<String>,
    port_override: Option<u16>,
) -> Result<i32, String> {
    let mut cfg = config::Config::load(config_path.as_deref())?;
    if let Some(host) = host_override {
        cfg.host = host;
    }
    if let Some(port) = port_override {
        cfg.port = port;
    }
    let listener = tokio::net::TcpListener::bind((cfg.host.as_str(), cfg.port))
        .await
        .map_err(|e| format!("bind {}:{} failed: {e}", cfg.host, cfg.port))?;
    tracing::info!(
        host = %cfg.host,
        port = cfg.port,
        version = app::ENGINE_VERSION,
        "fsm-server listening"
    );
    axum::serve(listener, app::app(cfg.budget))
        .with_graceful_shutdown(shutdown_signal())
        .await
        .map_err(|e| format!("server error: {e}"))?;
    Ok(0)
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    tracing::info!("shutdown signal received");
}

#[allow(clippy::too_many_arguments)]
fn check_file(
    config_path: Option<String>,
    path: &str,
    max_states: Option<u64>,
    max_transitions: Option<u64>,
    max_initial_scan: Option<u64>,
) -> Result<i32, String> {
    let cfg = config::Config::load(config_path.as_deref())?;
    let text = std::fs::read_to_string(path).map_err(|e| format!("read {path}: {e}"))?;
    let raw: serde_json::Value =
        serde_json::from_str(&text).map_err(|e| format!("invalid JSON in {path}: {e}"))?;

    // Accept either a bare spec or {"spec": ..., "budget": ...}.
    let (spec_value, file_budget) = if raw.get("spec").is_some() {
        let spec = raw["spec"].clone();
        let b = raw.get("budget").cloned();
        (spec, b)
    } else {
        (raw, None)
    };
    let spec: Spec = serde_json::from_value(spec_value)
        .map_err(|e| format!("specification schema error: {e}"))?;
    let (fingerprint_hex, _) =
        fingerprint::fingerprint(&spec).map_err(|e| format!("fingerprint error: {e}"))?;
    let compiled = CompiledSpec::compile(&spec).map_err(|e| {
        let payload = json!({
            "status": "invalid",
            "reason": e.code,
            "failure": { "code": e.code, "message": e.message },
        });
        println!("{}", serde_json::to_string_pretty(&payload).unwrap());
        format!("{}: {}", e.code, e.message)
    })?;

    let budget = Budget {
        max_states: max_states
            .or_else(|| file_budget.as_ref().and_then(|b| b["max_states"].as_u64()))
            .unwrap_or(cfg.budget.max_states),
        max_transitions: max_transitions
            .or_else(|| file_budget.as_ref().and_then(|b| b["max_transitions"].as_u64()))
            .unwrap_or(cfg.budget.max_transitions),
        max_initial_scan: max_initial_scan
            .or_else(|| file_budget.as_ref().and_then(|b| b["max_initial_scan"].as_u64()))
            .unwrap_or(cfg.budget.max_initial_scan),
    };

    tracing::info!(spec = %compiled.name, fingerprint = %fingerprint_hex, "check start");
    let outcome = explore(&compiled, &budget);

    let mut evidence_checks = Vec::new();
    for p in &outcome.properties {
        if let Some(ev) = &p.evidence {
            let report = replay(&compiled, ev);
            evidence_checks.push(json!({
                "property": p.name,
                "kind": ev.kind,
                "replay_valid": report.valid,
                "steps": report.steps.len(),
                "failure": report.failure,
            }));
        }
    }
    for ev in outcome
        .deadlock_evidence
        .iter()
        .chain(outcome.terminal_evidence.iter())
    {
        let report = replay(&compiled, ev);
        evidence_checks.push(json!({
            "property": null,
            "kind": ev.kind,
            "replay_valid": report.valid,
            "steps": report.steps.len(),
            "failure": report.failure,
        }));
    }

    let payload = json!({
        "engine_version": app::ENGINE_VERSION,
        "algorithm": app::ALGORITHM,
        "spec_name": compiled.name,
        "spec_fingerprint": fingerprint_hex,
        "status": outcome.status,
        "reason": outcome.reason,
        "truncated": outcome.truncated,
        "stats": outcome.stats,
        "properties": outcome.properties,
        "deadlocks": outcome.deadlock_evidence,
        "terminals": outcome.terminal_evidence,
        "evidence_checks": evidence_checks,
        "failure": outcome.error,
    });
    println!("{}", serde_json::to_string_pretty(&payload).unwrap());

    Ok(match outcome.status {
        RunStatus::Complete => 0,
        RunStatus::Invalid => 2,
        RunStatus::Error => 3,
        RunStatus::Truncated => 4,
    })
}
