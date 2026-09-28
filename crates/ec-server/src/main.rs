//! Service entry point.
//!
//! Usage:
//! ```text
//! ec-server --config config/service.example.json
//! ```
//! Startup logs every assumption (field, coding, storage location) so a run
//! is self-describing; the service binds only after the store opens and the
//! coding parameters validate.

use std::sync::Arc;

use anyhow::{bail, Context, Result};
use ec_api::http::AppState;
use ec_api::{router, ErasureCodingService, ServerConfig};
use ec_core::CodecConfig;
use ec_gf::prewarm as gf_prewarm;
use ec_store::FileSystemStore;
use tracing::{info, warn};

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let config_path = match args.get(1) {
        Some(p) if p == "--config" => args
            .get(2)
            .context("missing path after --config")?
            .clone(),
        Some(other) => {
            bail!("unsupported argument {other}; usage: ec-server --config <path>")
        }
        None => "config/service.example.json".to_string(),
    };

    let cfg = ServerConfig::load(&config_path)
        .with_context(|| format!("loading config {config_path}"))?;

    init_tracing(&cfg.log_level);

    info!("erasure-coding service starting");
    info!("config file      : {}", std::path::Path::new(&config_path).display());
    info!("listen address   : {}", cfg.listen);
    info!("storage dir      : {}", cfg.storage_dir);
    info!("coding params    : RS(k={},m={})", cfg.k, cfg.m);
    info!("field definition : GF(2^8) mod 0x11B (AES poly), generator 3");
    info!("coding matrix    : systematic [I_k | Cauchy]");
    info!("integrity        : per-shard SHA-256 incl. position, manifest digest over TLV covered set");

    let codec = CodecConfig::new(cfg.k, cfg.m).context("invalid coding parameters")?;
    let store = FileSystemStore::open(&cfg.storage_dir)
        .with_context(|| format!("opening storage dir {}", cfg.storage_dir))?;
    let service = ErasureCodingService::new(Arc::new(store));

    // Build field tables eagerly and print a sanity marker.
    gf_prewarm();
    info!("GF tables built (255 nonzero elements enumerated; 2=0x02, mul(0x57,0x83)={:#04x})", ec_gf::mul(0x57, 0x83));

    if !cfg.allow_writes {
        warn!("allow_writes=false: encode/repair endpoints will reject requests");
    }

    let listener = tokio::net::TcpListener::bind(&cfg.listen)
        .await
        .with_context(|| format!("binding {}", cfg.listen))?;
    info!("listening on {} (request ids via x-request-id header)", cfg.listen);

    let app = router(AppState {
        service,
        cfg: codec,
        allow_writes: cfg.allow_writes,
    });

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .context("HTTP server failed")?;
    info!("shutdown complete");
    Ok(())
}

fn init_tracing(level: &str) {
    use tracing::level_filters::LevelFilter;
    use tracing_subscriber::fmt;
    // No `env-filter` feature is needed: the config exposes one flat level.
    let filter = match level.to_ascii_lowercase().as_str() {
        "trace" => LevelFilter::TRACE,
        "debug" => LevelFilter::DEBUG,
        "info" => LevelFilter::INFO,
        "warn" => LevelFilter::WARN,
        "error" | "off" => LevelFilter::ERROR,
        _ => LevelFilter::INFO,
    };
    fmt()
        .with_max_level(filter)
        .with_target(false)
        // Compact line keeps request_id / object_id readable in terminals.
        .compact()
        .init();
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("SIGINT received, draining");
}
