//! symexd: HTTP server for the symbolic execution service.

use std::path::PathBuf;

use symex::api::build_router;
use symex::config::ConfigFile;

#[tokio::main]
async fn main() {
    let mut args = std::env::args().skip(1);
    let mut config_path: Option<PathBuf> = None;
    while let Some(a) = args.next() {
        match a.as_str() {
            "--config" | "-c" => {
                let Some(p) = args.next() else {
                    eprintln!("--config requires a path");
                    std::process::exit(2);
                };
                config_path = Some(PathBuf::from(p));
            }
            "--help" | "-h" => {
                println!("symexd [--config PATH]");
                println!("  Serves POST /v1/analyze, POST /v1/replay, GET /v1/health.");
                return;
            }
            other => {
                eprintln!("unknown argument: {other}");
                std::process::exit(2);
            }
        }
    }

    let mut cfg = match &config_path {
        Some(p) => match std::fs::read_to_string(p) {
            Ok(text) => match ConfigFile::from_toml(&text) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("config error in {}: {e}", p.display());
                    std::process::exit(2);
                }
            },
            Err(e) => {
                eprintln!("cannot read {}: {e}", p.display());
                std::process::exit(2);
            }
        },
        None => ConfigFile::default(),
    };
    if let Err(e) = cfg.apply_env() {
        eprintln!("environment override error: {e}");
        std::process::exit(2);
    }

    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new(&cfg.server.log_level));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(true)
        .init();

    let bind = cfg.server.bind.clone();
    let router = build_router(cfg);
    let listener = match tokio::net::TcpListener::bind(&bind).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("cannot bind {bind}: {e}");
            std::process::exit(1);
        }
    };
    tracing::info!(bind = %bind, z3 = %symex::z3_version(), "symexd listening");
    if let Err(e) = axum::serve(listener, router).await {
        eprintln!("server error: {e}");
        std::process::exit(1);
    }
}
