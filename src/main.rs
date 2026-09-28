//! Server binary: loads configuration, restores persisted indexes from the
//! data directory and serves the validation API.

use std::path::PathBuf;

use tracing_subscriber::EnvFilter;
use wavelet_matrix_service::api::{serve, AppState};
use wavelet_matrix_service::config::Config;
use wavelet_matrix_service::error::WmError;
use wavelet_matrix_service::store::IndexStore;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let config_path = parse_config_arg(&args).unwrap_or_else(|msg| {
        eprintln!("error: {msg}");
        eprintln!("usage: {} [--config <path>]", args[0]);
        std::process::exit(2);
    });

    // A missing default config falls back to built-in defaults; a missing
    // explicitly named config is a hard failure.
    let explicit = config_path.is_some();
    let path = config_path.unwrap_or_else(|| PathBuf::from("config/default.toml"));
    let config = match Config::load(&path) {
        Ok(cfg) => cfg,
        Err(WmError::Io(_)) if !explicit => Config::defaults(),
        Err(e) => {
            eprintln!("error loading config {}: {e}", path.display());
            std::process::exit(1);
        }
    };

    init_tracing(&config.log_level);

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime");

    runtime.block_on(async move {
        tracing::info!(
            config_path = %path.display(),
            listen = %config.listen,
            data_dir = %config.data_dir.display(),
            "configuration loaded"
        );
        let store = match IndexStore::new(&config.data_dir) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("fatal: cannot open data directory: {e}");
                std::process::exit(1);
            }
        };
        let state = AppState::new(store);
        match state.load_persisted() {
            Ok(n) if n > 0 => tracing::info!("restored {n} persisted index(es)"),
            Ok(_) => tracing::info!("no persisted indexes found, starting empty"),
            Err(e) => {
                eprintln!("fatal: cannot restore indexes: {e}");
                std::process::exit(1);
            }
        }

        let router = state.build_router();
        if let Err(e) = serve(router, config.listen).await {
            eprintln!("fatal: {e}");
            std::process::exit(1);
        }
    });
}

fn parse_config_arg(args: &[String]) -> Result<Option<PathBuf>, String> {
    let mut path = None;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--config" => {
                i += 1;
                let v = args
                    .get(i)
                    .ok_or_else(|| "--config requires a path argument".to_string())?;
                path = Some(PathBuf::from(v));
            }
            other => return Err(format!("unknown argument {other:?}")),
        }
        i += 1;
    }
    Ok(path)
}

fn init_tracing(level: &str) {
    let filter = EnvFilter::try_new(level).unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .init();
}
