use std::sync::Arc;

use mph_service::api::{self, AppState};
use mph_service::config::ServiceConfig;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "mph_service=info".into()),
        )
        .init();

    let config_path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "config/service.json".to_string());
    let cfg = ServiceConfig::load(&config_path).unwrap_or_else(|e| {
        tracing::warn!(error = %e, "falling back to default config");
        ServiceConfig::default()
    });
    tracing::info!(config = %config_path, listen = %cfg.listen, "starting mph-service");

    // Load a previously persisted index if present; absence is fine.
    let index_path = std::path::PathBuf::from(&cfg.index_path);
    let initial = match mph_service::store::load(&index_path) {
        Ok(idx) => {
            tracing::info!(path = %cfg.index_path, n = idx.n, seed = idx.seed, "loaded persisted index");
            Some(idx)
        }
        Err(e) => {
            tracing::info!(path = %cfg.index_path, reason = %e, "no persisted index loaded");
            None
        }
    };

    let state = Arc::new(AppState {
        index: std::sync::RwLock::new(initial),
        cfg: cfg.clone(),
        req_counter: std::sync::atomic::AtomicU64::new(0),
    });

    let listener = tokio::net::TcpListener::bind(&cfg.listen)
        .await
        .expect("bind listen address");
    tracing::info!(listen = %cfg.listen, "serving");
    axum::serve(listener, api::router(state)).await.unwrap();
}
