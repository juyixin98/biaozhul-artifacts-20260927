//! Binary entry point: wires configuration, logging and the Axum server.

use std::sync::Arc;

use pn_server::config::Config;
use pn_server::http::{router, AppState};
use tracing::info;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() {
    let config_path =
        std::env::var("PN_CONFIG_FILE").unwrap_or_else(|_| "config/server.conf".into());
    let config = Config::load(Some(config_path)).unwrap_or_else(|e| {
        eprintln!("configuration error: {e}; using built-in defaults");
        Config::default()
    });

    init_tracing(&config.log_level);

    let bind = config.http_bind.clone();
    let version = config.server_version.clone();
    let state = AppState {
        config: Arc::new(config),
    };

    let app = router(state);
    let listener = tokio::net::TcpListener::bind(&bind)
        .await
        .unwrap_or_else(|e| panic!("failed to bind {bind}: {e}"));

    info!(
        bind = %bind,
        version = %version,
        schema = "petri-analysis/v1",
        model = "bounded-capacity ordinary weighted Petri net",
        unbounded_reachability_complete = false,
        "petri reachability backend listening"
    );

    axum::serve(listener, app).await.expect("server error");
}

fn init_tracing(level: &str) {
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new(format!("warn,pn_server={level},tower_http={level}")));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(true)
        .with_level(true)
        .init();
}
