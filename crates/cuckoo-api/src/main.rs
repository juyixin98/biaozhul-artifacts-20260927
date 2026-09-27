//! cuckoo-api 入口：加载配置、打开或初始化快照、启动 Axum 服务。
//!
//! 初始化策略：快照不存在则按配置创建；存在则必须与配置参数完全一致，
//! 否则拒绝启动（参数决定存储格式，不允许“静默换内核”打开旧数据）。

use std::path::PathBuf;

use cuckoo_api::server::{build_state, init_tracing, log_startup, serve};
use cuckoo_api::config::Config;
use tracing::error;

#[tokio::main]
async fn main() {
    init_tracing();

    let cfg_path = std::env::args().nth(1).map(PathBuf::from);
    let cfg = match Config::load(cfg_path.as_deref()) {
        Ok(c) => c,
        Err(e) => {
            error!("配置无效: {e}");
            std::process::exit(1);
        }
    };

    log_startup(&cfg);

    let state = match build_state(&cfg) {
        Ok(s) => s,
        Err(e) => {
            error!(error = %e, "初始化状态失败");
            std::process::exit(1);
        }
    };

    if let Err(e) = serve(cfg, state).await {
        error!(error = %e, "服务异常退出");
        std::process::exit(1);
    }
}
