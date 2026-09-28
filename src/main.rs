//! 二进制入口：装配日志、配置与 Axum 服务。

use std::sync::Arc;

use cnf_solver_backend::api::{router, AppState};
use cnf_solver_backend::config::Config;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let config = Config::from_env()?;

    if config.json_logs {
        tracing_subscriber::fmt()
            .json()
            .with_env_filter(
                EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
            )
            .init();
    } else {
        tracing_subscriber::fmt()
            .with_env_filter(
                EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
            )
            .init();
    }

    let bind = config.bind.clone();
    let listener = tokio::net::TcpListener::bind(&bind).await?;
    tracing::info!(bind = %bind, "CNF solver backend listening");

    let app = router(AppState {
        config: Arc::new(config),
    });
    axum::serve(listener, app).await?;
    Ok(())
}
