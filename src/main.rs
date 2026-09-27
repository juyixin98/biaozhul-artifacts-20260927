//! Binary entry point: resolve configuration, install tracing, serve.
//!
//! Usage:
//!
//! ```text
//! bdd-server [--config config/dev.toml]
//! ```

use std::path::PathBuf;
use std::sync::Arc;

use bdd_backend::api::AppState;
use bdd_backend::config::Config;

#[derive(Debug)]
struct Args {
    config: Option<PathBuf>,
}

fn parse_args() -> Result<Args, String> {
    let mut config = None;
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--config" => {
                let p = it
                    .next()
                    .ok_or_else(|| "--config requires a path".to_string())?;
                config = Some(PathBuf::from(p));
            }
            "--help" | "-h" => {
                println!("Usage: bdd-server [--config <path>]");
                std::process::exit(0);
            }
            other => return Err(format!("unknown argument {other:?}")),
        }
    }
    Ok(Args { config })
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = parse_args().map_err(|e| {
        eprintln!("{e}");
        e
    })?;
    let config: Config = Config::load(args.config.as_deref())?;

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_new(&config.log_level)
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let bind = config.bind_addr.clone();
    let state = Arc::new(AppState::new(config));
    let app = bdd_backend::api::app(state);

    let listener = tokio::net::TcpListener::bind(&bind).await?;
    tracing::info!(%bind, "ROBDD backend listening");
    axum::serve(listener, app).await?;
    Ok(())
}
