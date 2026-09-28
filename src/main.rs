//! 服务入口：装载配置、初始化日志、启动 Axum。

use std::sync::Arc;

use petri_reach::api::build_router;
use petri_reach::config::Config;
use tracing_subscriber::{fmt, EnvFilter};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let config = Config::load()?;

    // 以配置文件 log_level 为基准；若设置了 RUST_LOG 则其优先覆盖。
    let filter = std::env::var("RUST_LOG")
        .ok()
        .filter(|s| !s.is_empty())
        .map(EnvFilter::new)
        .unwrap_or_else(|| EnvFilter::new(&config.log_level));
    fmt()
        .with_env_filter(filter)
        .with_target(true)
        .with_thread_ids(false)
        .init();

    let bind = config.bind_address();
    let version = env!("CARGO_PKG_VERSION");
    tracing::info!(
        service = "petri-reach",
        version,
        bind = %bind,
        state_limit = config.solver_state_limit,
        coefficient_bound = config.invariant_coefficient_bound,
        "starting petri-reach"
    );

    let listener = tokio::net::TcpListener::bind(&bind).await?;
    tracing::info!("listening on http://{bind}");
    axum::serve(listener, build_router(Arc::new(config)))
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
        if let Ok(mut sig) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            sig.recv().await;
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
    tracing::info!("shutdown signal received");
}
