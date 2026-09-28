//! # rb-server
//!
//! 分层位图集合的 HTTP 验证接口（Axum）。
//!
//! 设计要点：
//!
//! - 每个响应都带 `x-request-id`（客户端可用同名请求头指定，否则服务端生成），
//!   日志的每条记录都带该标识，响应体回显，便于关联一次请求；
//! - 统一响应信封 [`model::Envelope`]：`result` 放成功结果，`errors` 单列失败原因，
//!   `notes` 单列不确定/需人工关注的结论，`steps` 展示关键处理步骤、
//!   数据格式版本与处理位置（哪个高 16 位分片等）；
//! - 文件损坏不返回 500，而是 422 + 具体错误代码（与 [`rb_format::CodecError`]
//!   一一对应），调用方可以断言失败类别；
//! - 阻塞的文件系统操作放在 `spawn_blocking` 中。

#![forbid(unsafe_code)]

pub mod config;
pub mod error;
pub mod handlers;
pub mod model;
pub mod request_id;
pub mod state;

use axum::middleware;
use tracing::info;

use crate::config::Config;
use crate::request_id::mw as request_id_mw;
use crate::state::AppState;

/// 组装完整应用（带请求 id 中间件与共享状态）。
pub fn app(state: AppState) -> axum::Router {
    handlers::router()
        .layer(middleware::from_fn(request_id_mw))
        .with_state(state)
}

/// 进程入口：读配置、打开存储、启动 HTTP 服务。
pub async fn run() -> anyhow::Result<()> {
    let config = Config::from_env();
    init_tracing(&config);

    let store = rb_persist::Store::open(&config.data_dir)
        .map_err(|e| anyhow::anyhow!("failed to open data dir {:?}: {e}", config.data_dir))?;
    let state = AppState::new(store);

    let addr: std::net::SocketAddr = config.bind.parse()?;
    let listener = tokio::net::TcpListener::bind(addr).await?;
    info!(
        bind = %addr,
        data_dir = %config.data_dir.display(),
        format_version = rb_format::FORMAT_VERSION,
        "rb-server listening"
    );

    axum::serve(listener, app(state)).await?;
    Ok(())
}

fn init_tracing(config: &Config) {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_new(&config.log_level)
        .unwrap_or_else(|_| EnvFilter::new("info,rb_server=debug"));
    fmt()
        .with_env_filter(filter)
        .with_target(true)
        .json()
        .init();
}
