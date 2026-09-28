//! `hcompd` — HTTP server binary for the hcomp codec backend.
//!
//! Usage:
//! ```text
//! hcompd [--config <path-to-toml>]
//! ```
//! Every setting can additionally be overridden with `HCOMP_*` environment
//! variables; see [`hcomp::config::Config`].

use hcomp::api::{router, AppState};
use hcomp::config::Config;
use hcomp::store::FileSystemStore;
use std::sync::Arc;
use tracing::{error, info};
use tracing_subscriber::{fmt, EnvFilter};

fn usage() -> ! {
    eprintln!("usage: hcompd [--config <hcomp.toml>]");
    std::process::exit(2);
}

#[tokio::main]
async fn main() {
    if let Err(code) = run().await {
        error!(error = %code, "fatal");
        std::process::exit(1);
    }
}

async fn run() -> Result<(), String> {
    let mut config_path: Option<String> = None;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--config" => {
                config_path = Some(args.next().unwrap_or_else(|| usage()));
            }
            "-h" | "--help" => {
                println!("usage: hcompd [--config <hcomp.toml>]");
                println!();
                println!("Environment overrides: HCOMP_STORE_DIR, HCOMP_MAX_BLOCK_LEN,");
                println!("HCOMP_MAX_BODY_LEN, HCOMP_BIND_ADDR, HCOMP_LOG_FORMAT, RUST_LOG");
                return Ok(());
            }
            other => {
                eprintln!("unknown argument: {other}");
                usage();
            }
        }
    }

    let cfg = Config::load(config_path.as_deref()).map_err(|e| e.to_string())?;

    init_tracing(&cfg.log_format);
    info!(
        version = hcomp::VERSION,
        store_dir = %cfg.store_dir,
        bind_addr = %cfg.bind_addr,
        max_block_len = cfg.max_block_len,
        max_body_len = cfg.max_body_len,
        "starting hcompd"
    );

    let store = FileSystemStore::new(&cfg.store_dir)
        .await
        .map_err(|e| format!("opening store dir {}: {e}", cfg.store_dir))?;
    let state = AppState::new(cfg.clone(), Arc::new(store));
    let app = router(state);

    let listener = tokio::net::TcpListener::bind(&cfg.bind_addr)
        .await
        .map_err(|e| format!("binding {}: {e}", cfg.bind_addr))?;
    info!(bind_addr = %cfg.bind_addr, "listening");
    axum::serve(listener, app)
        .await
        .map_err(|e| format!("server error: {e}"))?;
    Ok(())
}

fn init_tracing(format: &str) {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    match format {
        "json" => {
            let _ = fmt().json().with_env_filter(filter).try_init();
        }
        "debug" => {
            let _ = fmt().pretty().with_env_filter(filter).try_init();
        }
        _ => {
            let _ = fmt().with_env_filter(filter).try_init();
        }
    }
}
