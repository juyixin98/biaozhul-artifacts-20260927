//! HTTP 服务引导逻辑（二进制入口与集成测试共用）。

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use cuckoo_persist::ServiceState;
use tracing::info;

use crate::app::{router, AppState};
use crate::config::Config;

pub fn init_tracing() {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_from_env("CKF_LOG")
        .unwrap_or_else(|_| EnvFilter::new("info,cuckoo_api=debug,cuckoo_persist=info"));
    fmt().with_env_filter(filter).with_target(true).init();
}

pub fn run_id() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("run-{:x}", nanos)
}

/// 依据配置打开或创建服务状态。
pub fn build_state(cfg: &Config) -> Result<AppState, Box<dyn std::error::Error>> {
    let exists = cfg.snapshot_path.exists();
    let svc = if exists {
        info!(path = %cfg.snapshot_path.display(), "发现快照，按配置参数打开");
        ServiceState::open(
            &cfg.snapshot_path,
            cfg.params,
            cfg.master_key.clone(),
            cfg.rng_seed,
        )?
    } else {
        info!(path = %cfg.snapshot_path.display(), "快照不存在，首次初始化");
        ServiceState::create(
            &cfg.snapshot_path,
            cfg.params,
            cfg.master_key.clone(),
            cfg.rng_seed,
        )?
    };
    Ok(AppState {
        svc: Arc::new(Mutex::new(svc)),
        started_at: std::time::SystemTime::now(),
        run_id: run_id(),
    })
}

pub fn log_startup(cfg: &Config) {
    info!(
        version = cuckoo_core::CORE_VERSION,
        format_version = cuckoo_core::FORMAT_VERSION,
        bind = %cfg.bind,
        snapshot = %cfg.snapshot_path.display(),
        key_source = %cfg.key_source,
        buckets_exp = cfg.params.buckets_exp,
        bucket_size = cfg.params.bucket_size,
        fingerprint_bits = cfg.params.fingerprint_bits,
        max_kicks = cfg.params.max_kicks,
        rng_seed = cfg.rng_seed,
        "启动配置（不含密钥内容）"
    );
}

/// 绑定并开始服务（直到收到停机信号）。
pub async fn serve(cfg: Config, state: AppState) -> Result<(), Box<dyn std::error::Error>> {
    let listener = tokio::net::TcpListener::bind(&cfg.bind).await?;
    info!(addr = %listener.local_addr()?, "HTTP 服务开始监听");
    axum::serve(listener, router(state))
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    info!("已优雅退出");
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("安装 Ctrl-C 处理器失败");
    };

    #[cfg(unix)]
    let terminate = async {
        let mut sig =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .expect("安装 SIGTERM 处理器失败");
        sig.recv().await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
    info!("收到停机信号，开始优雅关闭");
}

/// 仅测试用：从显式参数构造配置。
pub fn config_for_test(
    snapshot_path: PathBuf,
    master_key: Vec<u8>,
    params: cuckoo_core::FilterParams,
    rng_seed: u64,
) -> Config {
    Config {
        bind: "127.0.0.1:0".to_string(),
        params,
        rng_seed,
        snapshot_path,
        master_key,
        key_source: "test".to_string(),
    }
}

/// 集成测试辅助：在临时目录上构建 AppState。
pub fn test_state(
    dir: &Path,
    params: cuckoo_core::FilterParams,
) -> Result<AppState, Box<dyn std::error::Error>> {
    let cfg = config_for_test(
        dir.join("snapshot.bin"),
        b"test-master-key-0123456789abcd".to_vec(),
        params,
        0x7E57_5EED_2026_0928,
    );
    build_state(&cfg)
}
