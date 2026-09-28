//! Server entry point.
//!
//! Binds `127.0.0.1:8080` by default; override with `BIND_ADDR`
//! (e.g. `BIND_ADDR=0.0.0.0:9000`). The service starts with an empty
//! constraint set and keeps state in memory.

use std::sync::Arc;

use diffconstraints::http_api;
use diffconstraints::ConstraintService;
use tracing::Level;

#[tokio::main]
async fn main() {
    init_tracing();

    let addr = std::env::var("BIND_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".to_string());
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .unwrap_or_else(|e| panic!("failed to bind {addr}: {e}"));
    tracing::info!(%addr, "difference-constraint service listening");

    let service = Arc::new(ConstraintService::new());
    let app = http_api::app(service);

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .expect("server error");
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
            .expect("install SIGTERM handler")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
    tracing::info!("shutdown signal received, draining");
}

/// Minimal level selection: honors a global `RUST_LOG` level
/// (`error|warn|info|debug|trace`); defaults to `debug` so request logs and
/// solver details are visible locally.
fn init_tracing() {
    let level = std::env::var("RUST_LOG")
        .ok()
        .and_then(|s| parse_level(s.trim()))
        .unwrap_or(Level::DEBUG);
    tracing_subscriber::fmt()
        .with_max_level(level)
        .with_target(true)
        .with_level(true)
        .init();
}

fn parse_level(s: &str) -> Option<Level> {
    Some(match s.to_ascii_lowercase().as_str() {
        "error" => Level::ERROR,
        "warn" => Level::WARN,
        "info" => Level::INFO,
        "debug" => Level::DEBUG,
        "trace" => Level::TRACE,
        _ => return None,
    })
}
