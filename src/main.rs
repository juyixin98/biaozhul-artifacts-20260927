//! HTTP 服务入口：`cnf-api`。

use std::net::SocketAddr;

use cnf_dpll::api::build_router;
use cnf_dpll::config::AppConfig;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,cnf_dpll=debug".parse().unwrap()),
        )
        .init();

    let config = AppConfig::from_env();
    let bind: SocketAddr = config
        .bind
        .parse()
        .unwrap_or_else(|e| panic!("非法监听地址 {}: {e}", config.bind));
    let app = build_router(AppConfig {
        bind: bind.to_string(),
        ..config
    });

    let listener = tokio::net::TcpListener::bind(bind)
        .await
        .unwrap_or_else(|e| panic!("无法绑定 {bind}: {e}"));
    tracing::info!(%bind, "CNF DPLL 服务启动");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .expect("服务异常退出");
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    tracing::info!("收到 Ctrl-C，开始优雅停机");
}
