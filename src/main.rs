//! Binary entry point: load configuration, initialize logging and serve.

use ec_service::{api, build_state, Config};
use tracing_subscriber::{fmt, EnvFilter};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // RUST_LOG overrides; default to info for our crate and warn for deps.
    let filter = EnvFilter::try_from_env("RUST_LOG")
        .unwrap_or_else(|_| EnvFilter::new("info,ec_service=debug,tower_http=off"));
    fmt().with_env_filter(filter).with_target(true).init();

    let config = Config::from_env();
    tracing::info!(
        data_dir = %config.data_dir,
        bind = %config.bind_addr,
        max_object_bytes = config.max_object_bytes,
        profiles = ?config.allowed_profiles,
        "starting ec-service"
    );

    let state = build_state(&config).await?;
    let app = api::router(state);

    let listener = tokio::net::TcpListener::bind(&config.bind_addr).await?;
    tracing::info!(addr = %config.bind_addr, "listening");
    axum::serve(listener, app).await?;
    Ok(())
}
