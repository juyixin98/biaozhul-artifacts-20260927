//! Service entry point.

use std::path::PathBuf;

use clap::Parser;

use wm_server::{build_app, Config};

/// Wavelet matrix verification service.
#[derive(Parser, Debug)]
#[command(name = "wm-server", version, about)]
struct Cli {
    /// TOML configuration file.
    #[arg(long, value_name = "FILE")]
    config: Option<PathBuf>,
    /// Directory for persisted `<name>.wmi` indexes.
    #[arg(long)]
    data_dir: Option<PathBuf>,
    /// Bind host.
    #[arg(long)]
    host: Option<String>,
    /// Bind port.
    #[arg(long)]
    port: Option<u16>,
    /// Log filter directive (e.g. info, debug, wm_server=trace).
    #[arg(long)]
    log_level: Option<String>,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    let config = Config::load(
        cli.config.as_deref(),
        cli.host,
        cli.port,
        cli.data_dir,
        cli.log_level,
    )?;

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_new(&config.log_level)
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .json()
        .init();

    let router = build_app(&config.data_dir)?;
    let addr = format!("{}:{}", config.host, config.port);
    let listener = tokio::net::TcpListener::bind(&addr).await?;

    tracing::info!(
        addr = %listener.local_addr()?,
        data_dir = %config.data_dir.display(),
        config_source = ?config.source,
        format_version = wm_format::FORMAT_VERSION,
        "wavelet matrix service starting"
    );
    println!("listening on http://{}", listener.local_addr()?);

    axum::serve(listener, router).await?;
    Ok(())
}
