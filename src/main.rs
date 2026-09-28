//! 服务入口：`weak_trace_server [--bind 127.0.0.1:8080]`。
//!
//! 环境变量：
//! * `BIND_ADDR`：监听地址（默认 127.0.0.1:8080）；
//! * `RUST_LOG`：日志级别（默认 info）。

use std::net::SocketAddr;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new("info,weak_trace_inclusion=debug")),
        )
        .init();

    let bind = std::env::args()
        .zip(std::env::args().skip(1))
        .find_map(|(a, b)| (a == "--bind").then_some(b))
        .or_else(|| std::env::var("BIND_ADDR").ok())
        .unwrap_or_else(|| "127.0.0.1:8080".to_owned());

    let addr: SocketAddr = bind.parse().unwrap_or_else(|e| {
        eprintln!("无法解析监听地址 {bind:?}: {e}");
        std::process::exit(2);
    });

    let listener = tokio::net::TcpListener::bind(addr).await.unwrap_or_else(|e| {
        eprintln!("无法绑定 {addr}: {e}");
        std::process::exit(1);
    });
    tracing::info!("弱迹包含检查服务监听 http://{addr}");
    tracing::info!("接口：GET /health，POST /check");

    axum::serve(listener, weak_trace_inclusion::service::router())
        .await
        .unwrap_or_else(|e| {
            eprintln!("服务错误: {e}");
            std::process::exit(1);
        });
}
