//! 服务入口：加载配置 → 初始化日志 → 绑定监听 → 启动 Axum。

use robdd::backend::config::Config;
use robdd::{build_router, AppState};
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // 跳过程序名，其余作为配置覆盖参数。
    let cli: Vec<String> = std::env::args().skip(1).collect();
    let cfg = Config::load(&cli)?;

    init_tracing(&cfg.log_level);

    let bind = cfg.bind.clone();
    let state = AppState::new();
    let app = build_router(state, cfg.var_cap);

    let listener = tokio::net::TcpListener::bind(&bind).await?;
    let local = listener.local_addr()?;
    tracing::info!(
        var_cap = cfg.var_cap,
        redact = cfg.redact_by_default,
        "ROBDD service listening on http://{local}"
    );
    println!("ROBDD service listening on http://{local}");

    axum::serve(listener, app).await?;
    Ok(())
}

fn init_tracing(level: &str) {
    // 不依赖 tracing_subscriber 的 env-filter feature，手工映射级别。
    let filter = match level.to_ascii_lowercase().as_str() {
        "error" => EnvFilter::new("error"),
        "warn" => EnvFilter::new("warn"),
        "debug" => EnvFilter::new("debug,robdd=trace"),
        "trace" => EnvFilter::new("trace"),
        "off" => EnvFilter::new("off"),
        _ => EnvFilter::new("info,robdd=debug"),
    };
    tracing_subscriber::fmt().with_env_filter(filter).init();
}
