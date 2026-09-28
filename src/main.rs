//! Server entry point. Configuration comes from the environment — see
//! [`fm_index_svc::config::Config`].

use tracing_subscriber::{EnvFilter, fmt};

use fm_index_svc::config::Config;
use fm_index_svc::logging::RequestLog;
use fm_index_svc::service::{self, AppState};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cfg = Config::from_env()?;

    let filter = EnvFilter::try_new(&cfg.rust_log)
        .or_else(|_| EnvFilter::try_new("info"))
        .expect("valid fallback filter");
    fmt().with_env_filter(filter).init();

    tracing::info!(
        data_dir = %cfg.data_dir.display(),
        bind = %cfg.bind,
        max_text_bytes = cfg.max_text_bytes,
        default_sample_interval = cfg.default_sample_interval,
        log_dir = ?cfg.log_dir,
        "starting FM-index service"
    );

    let request_log = RequestLog::new(cfg.log_dir.as_deref());
    let state = AppState::new(cfg.clone())?.with_request_log(request_log);

    service::serve(state, &cfg.bind).await?;
    Ok(())
}
