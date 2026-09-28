//! HTTP server entrypoint.

use mus_core::{api::handlers::router, build_state, config::Config};
use tokio::net::TcpListener;
use tracing_subscriber::{fmt, EnvFilter};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cfg = Config::load()?;

    let filter = EnvFilter::try_new(&cfg.log_level)
        .unwrap_or_else(|_| EnvFilter::new("info"))
        // Never let a noisy dependency decide the floor.
        .add_directive("mus_core=info".parse()?);
    fmt().with_env_filter(filter).init();

    let bind = cfg.bind.clone();
    let state = build_state(cfg).map_err(std::io::Error::other)?;

    let listener = TcpListener::bind(&bind).await?;
    tracing::info!(%bind, "mus-core listening");
    axum::serve(listener, router(state)).await?;
    Ok(())
}
