//! pr2d-server 入口：配置 → 打开/重放 WAL → 绑定 Axum → 优雅关闭。

use std::sync::Arc;

use pr2d::api::{router, AppState};
use pr2d::config::Config;
use pr2d::store::Store;
use pr2d::telemetry::{init_tracing, RunIdentity};

#[tokio::main]
async fn main() {
    if let Err(e) = run().await {
        eprintln!("pr2d-server failed to start: {e}");
        std::process::exit(1);
    }
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let cfg = Config::load(&args)?;
    init_tracing(&cfg.log_level);

    // WAL 损坏是致命错误：拒绝以未知状态启动，而不是当成空库。
    let store = Store::open(std::path::Path::new(&cfg.data_dir)).map_err(|e| {
        eprintln!("refusing to start: {e}");
        e
    })?;
    let run = Arc::new(RunIdentity::new());
    tracing::info!(
        run_id = %run.run_id,
        data_dir = %cfg.data_dir,
        bind = %cfg.bind,
        tables = ?store.list_table_ids(),
        "pr2d starting"
    );

    let state = AppState {
        store: Arc::new(store),
        run,
        max_body_bytes: cfg.max_body_bytes,
    };
    let app = router(state);
    let listener = tokio::net::TcpListener::bind(&cfg.bind).await?;
    tracing::info!("pr2d listening on {}", cfg.bind);

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    tracing::info!("pr2d shut down");
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("install Ctrl-C handler");
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
}
