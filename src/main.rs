//! 服务入口：`cargo run` 启动 HTTP 服务（默认 127.0.0.1:8080）。

use std::net::SocketAddr;

use diff_constraints_service::api::{self, AppState};
use diff_constraints_service::store::ConstraintStore;

#[tokio::main]
async fn main() {
    let filter = std::env::var("RUST_LOG").unwrap_or_else(|_| {
        "info,diff_constraints_service=debug,diff_constraints=debug".to_string()
    });
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_new(&filter)
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let host = std::env::var("HOST").unwrap_or_else(|_| "127.0.0.1".to_string());
    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8080);
    let addr: SocketAddr = format!("{host}:{port}").parse().expect("invalid HOST:PORT");

    let state = AppState::new(ConstraintStore::new());
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("failed to bind listener");

    tracing::info!(
        target: "diff_constraints::server",
        %addr,
        "difference-constraints service listening (form: x - y <= c)"
    );
    axum::serve(listener, api::app(state))
        .with_graceful_shutdown(shutdown_signal())
        .await
        .expect("server error");
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    tracing::info!(target: "diff_constraints::server", "shutdown signal received");
}
