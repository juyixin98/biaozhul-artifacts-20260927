//! Server entry point.
//!
//! Usage: `prereg2d-server [path/to/config.toml]`
//! Environment overrides: `PREREG2D_DATA_DIR`, `PREREG2D_BIND`,
//! `PREREG2D_MAX_BODY_BYTES`.

use prereg2d::api;
use prereg2d::config::Config;
use prereg2d::service::Registry;
use std::path::PathBuf;
use std::sync::Arc;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_env("PREREG2D_LOG").unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_target(false)
        .init();

    let config_path = std::env::args().nth(1).map(PathBuf::from);
    let cfg = Config::load(config_path.as_deref())?;

    // Opening validates the log before we accept traffic: corrupt state or a
    // stale CURRENT aborts startup loudly instead of serving wrong answers.
    let registry = Arc::new(Registry::open(&cfg.data_dir)?);
    tracing::info!(
        data_dir = %cfg.data_dir.display(),
        head_version = ?registry.head_version(),
        "store opened"
    );

    let app = api::router(registry.clone(), cfg.max_body_bytes);
    let listener = tokio::net::TcpListener::bind(&cfg.bind).await?;
    tracing::info!(bind = %cfg.bind, "listening");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("failed to install Ctrl-C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
    tracing::info!("shutdown signal received");
}
