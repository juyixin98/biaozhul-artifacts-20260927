//! 结构化日志：统一版本与运行身份字段。
//!
//! `run_id` 作为根 span 的字段进入每条日志；各处理器再在其 span/事件中加入
//! `request_id`，从而「输入（请求）↔ 运行身份」可关联。

use tracing_subscriber::{fmt, prelude::*, EnvFilter};

/// 初始化日志订阅者。
pub fn init(level: &str, format: &str, run_id: &str) {
    let filter = EnvFilter::try_new(level).unwrap_or_else(|_| EnvFilter::new("info"));

    match format {
        "json" => {
            tracing_subscriber::registry()
                .with(filter)
                .with(fmt::layer().json().with_target(true))
                .init();
        }
        _ => {
            tracing_subscriber::registry()
                .with(filter)
                .with(fmt::layer().with_target(false))
                .init();
        }
    }

    let _enter = tracing::info_span!("service", run_id = %run_id).entered();
    tracing::info!(version = %crate::VERSION, "日志子系统就绪");
}
