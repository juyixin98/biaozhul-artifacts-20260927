//! `cf-svc` 二进制入口：加载配置、初始化日志与服务、启动 Axum。

use std::sync::Arc;

use deletable_cuckoo::{
    api, config::Config, service::Service, telemetry, KERNEL_VERSION, SNAPSHOT_FORMAT_VERSION,
    VERSION,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let config_path = std::env::args()
        .nth(1)
        .map(std::path::PathBuf::from)
        .or_else(|| Some(std::path::PathBuf::from("config/default.toml")));

    let cfg = Config::load(config_path.as_deref())?;

    let run_id = if cfg.logging.run_id.is_empty() {
        generate_run_id()
    } else {
        cfg.logging.run_id.clone()
    };
    telemetry::init(&cfg.logging.level, &cfg.logging.format, &run_id);

    tracing::info!(
        version = %VERSION,
        kernel_version = KERNEL_VERSION,
        snapshot_format_version = SNAPSHOT_FORMAT_VERSION,
        config_path = ?config_path,
        data_dir = %cfg.storage.data_dir.display(),
        "启动 deletable-cuckoo 服务"
    );

    let params = cfg.kernel_params();
    let service = Arc::new(Service::open(
        params,
        &cfg.storage.data_dir,
        &cfg.storage.snapshot_file,
        &cfg.credentials.hmac_secret_hex,
        cfg.storage.fsync,
        run_id.clone(),
        VERSION.to_string(),
        KERNEL_VERSION,
        SNAPSHOT_FORMAT_VERSION,
    )?);

    let app = api::router(service.clone(), run_id.clone());
    let addr = format!("{}:{}", cfg.server.host, cfg.server.port);
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    tracing::info!(%addr, "HTTP 监听就绪");
    println!("deletable-cuckoo {VERSION} listening on http://{addr} (run_id={run_id})");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    tracing::info!("收到关闭信号，执行最终落盘");
    if let Err(e) = service.force_flush() {
        tracing::error!(error = %e, "关闭时落盘失败");
    }
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c().await.expect("安装 Ctrl-C 处理器");
    };
    #[cfg(unix)]
    let terminate = async {
        if let Ok(mut s) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            s.recv().await;
        } else {
            std::future::pending::<()>().await;
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}

fn generate_run_id() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut buf = [0u8; 6];
    getrandom::getrandom(&mut buf).expect("rng");
    let h: String = buf.iter().map(|b| format!("{b:02x}")).collect();
    format!("run-{nanos:x}-{h}")
}
