//! Service entry point.
//!
//! Run with `cargo run` (optionally `MUS_CONFIG=path/config.toml`). See README.md for
//! configuration, examples and the support/scope statement.

use std::net::SocketAddr;

use mus_service::{build_state, router, Config};

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "mus_service=debug,tower_http=info".into()),
        )
        .init();

    let config_path =
        std::env::var("MUS_CONFIG").unwrap_or_else(|_| "config.toml".to_string());
    let config = match Config::load(&config_path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("configuration error: {e}");
            std::process::exit(2);
        }
    };

    let addr: SocketAddr = config
        .bind_addr
        .parse()
        .expect("MUS_BIND_ADDR / bind_addr must be an ip:port socket address");

    let state = build_state(config.clone());
    let app = router(state);

    tracing::info!(
        bind = %addr,
        default_solver = %config.default_solver,
        verifier = %config.verifier_solver,
        call_budget = ?config.default_call_budget,
        "mus-service starting"
    );

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .unwrap_or_else(|e| {
            eprintln!("failed to bind {addr}: {e}");
            std::process::exit(1);
        });
    axum::serve(listener, app)
        .await
        .expect("server error");
}
