//! CLI entry point: starts the Axum service using [`se_server::config::AppConfig`].

use std::net::SocketAddr;

use clap::Parser;
use se_solver::SmtSolver;
use se_server::api::AppState;
use se_server::config::AppConfig;
use se_server::app;

/// Bounded symbolic-execution service for small fixed-width integer programs.
#[derive(Parser, Debug)]
#[command(name = "se-server", version, about)]
struct Cli {
    /// Path to a TOML config file (optional; defaults are used otherwise).
    #[arg(long, value_name = "FILE")]
    config: Option<String>,
    /// Bind address (overrides config/env).
    #[arg(long)]
    bind: Option<String>,
    /// Listen port (overrides config/env).
    #[arg(long)]
    port: Option<u16>,
    /// Path to the z3 executable.
    #[arg(long)]
    z3_bin: Option<String>,
    /// Z3 per-query soft timeout in milliseconds.
    #[arg(long)]
    z3_timeout_ms: Option<u32>,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    let mut cfg = AppConfig::load(cli.config.as_deref())?;
    if let Some(b) = cli.bind {
        cfg.server.bind = b;
    }
    if let Some(p) = cli.port {
        cfg.server.port = p;
    }
    if let Some(z) = cli.z3_bin {
        cfg.solver.bin = z;
    }
    if let Some(t) = cli.z3_timeout_ms {
        cfg.solver.timeout_ms = t;
    }

    init_tracing(&cfg);

    let state = AppState::new(cfg.clone());
    if !state.solver.available() {
        tracing::error!(
            bin = %cfg.solver.bin,
            "SMT backend not found; install z3 or pass --z3-bin"
        );
    } else {
        tracing::info!(
            solver = state.solver.name(),
            version = state.solver.version(),
            "SMT backend ready"
        );
    }

    let body_limit = cfg.server.max_body_bytes;
    let addr: SocketAddr = format!("{}:{}", cfg.server.bind, cfg.server.port).parse()?;
    let router = app(state, body_limit);

    tracing::info!(%addr, "se-server listening");
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, router.into_make_service())
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

fn init_tracing(cfg: &AppConfig) {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_from_env("SE_LOG")
        .unwrap_or_else(|_| EnvFilter::new("info,se_server=debug"));
    if cfg.server.log_format == "json" {
        fmt().with_env_filter(filter).json().init();
    } else {
        fmt().with_env_filter(filter).init();
    }
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    tracing::info!("shutdown signal received");
}
