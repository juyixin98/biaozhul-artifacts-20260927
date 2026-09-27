//! 服务入口：参数解析 → 配置 → 日志 → 自动加载磁盘索引 → 启动 Axum。

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use fm_index_service::IndexService;
use fm_index_service::api;
use fm_index_service::config::Config;

#[derive(Debug, Default)]
struct CliArgs {
    config: Option<PathBuf>,
    host: Option<String>,
    port: Option<u16>,
    data_dir: Option<PathBuf>,
}

fn print_help() {
    println!(
        "fm-index-service —— 字节文本 FM 索引后端\n\
\n\
用法:\n  fm-index-service [选项]\n\
\n\
选项:\n\
  -c, --config <PATH>     配置文件路径（默认读 ./config.toml，不存在则用内置默认）\n\
  -H, --host <HOST>       覆盖监听地址（默认 127.0.0.1）\n\
  -p, --port <PORT>       覆盖监听端口（默认 8921）\n\
  -d, --data-dir <DIR>    覆盖索引存储目录（默认 ./data/indexes）\n\
  -h, --help              显示本帮助\n\
\n\
环境变量:\n\
  FM_INDEX_CONFIG         等价于 --config\n\
  RUST_LOG                日志级别（默认 info；可用 fm_index_service=debug）"
    );
}

fn parse_args() -> CliArgs {
    let mut args = CliArgs::default();
    let mut iter = std::env::args().skip(1);
    while let Some(a) = iter.next() {
        match a.as_str() {
            "-h" | "--help" => {
                print_help();
                std::process::exit(0);
            }
            "-c" | "--config" => {
                args.config = Some(PathBuf::from(iter.next().expect("--config 需要参数")))
            }
            "-H" | "--host" => args.host = Some(iter.next().expect("--host 需要参数")),
            "-p" | "--port" => {
                let v = iter.next().expect("--port 需要参数");
                args.port = Some(v.parse().expect("port 必须是数字"));
            }
            "-d" | "--data-dir" => {
                args.data_dir = Some(PathBuf::from(iter.next().expect("--data-dir 需要参数")))
            }
            other => {
                eprintln!("未知参数: {other}（用 --help 查看用法）");
                std::process::exit(2);
            }
        }
    }
    if args.config.is_none()
        && let Ok(env_path) = std::env::var("FM_INDEX_CONFIG")
    {
        args.config = Some(PathBuf::from(env_path));
    }
    args
}

fn init_tracing() {
    use tracing_subscriber::{EnvFilter, fmt, prelude::*};
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,fm_index_service=debug"));
    tracing_subscriber::registry()
        .with(filter)
        .with(fmt::layer().with_target(true))
        .init();
}

#[tokio::main]
async fn main() {
    init_tracing();
    let cli = parse_args();

    // 命令行 > 环境变量/文件 > 内置默认
    let cfg_path = cli.config.clone().or_else(|| {
        let local = PathBuf::from("config.toml");
        local.exists().then_some(local)
    });
    let cfg: Config = match Config::assemble(cfg_path.as_deref(), cli.host, cli.port, cli.data_dir)
    {
        Ok(c) => c,
        Err(e) => {
            eprintln!("配置错误: {e}");
            std::process::exit(2);
        }
    };

    std::fs::create_dir_all(&cfg.storage.data_dir).unwrap_or_else(|e| {
        eprintln!("无法创建数据目录 {:?}: {e}", cfg.storage.data_dir);
        std::process::exit(2);
    });

    let service = Arc::new(IndexService::new(
        cfg.storage.data_dir.clone(),
        cfg.storage.import_dirs.clone(),
        cfg.index.clone(),
        cfg.server.max_locations,
    ));

    // 启动时自动加载磁盘索引：单个损坏不阻塞启动，但显著告警。
    let (loaded, failed) = service.load_all_on_startup();
    for name in &loaded {
        tracing::info!(index = %name, "启动加载索引成功");
    }
    for (name, e) in &failed {
        tracing::error!(index = %name, error = %e, "启动加载索引失败（索引已隔离，需修复或删除后 /load）");
    }

    let app = api::app(service, cfg.server.max_body_bytes as usize);
    let addr: SocketAddr = format!("{}:{}", cfg.server.host, cfg.server.port)
        .parse()
        .unwrap_or_else(|e| {
            eprintln!("监听地址非法: {e}");
            std::process::exit(2);
        });

    tracing::info!(
        data_dir = ?cfg.storage.data_dir,
        import_dirs = ?cfg.storage.import_dirs,
        max_text_bytes = cfg.index.max_text_bytes,
        rank_block = cfg.index.rank_block,
        sample_step = cfg.index.sample_step,
        "FM 索引服务启动中，监听 http://{addr}（loaded={}, failed={}）",
        loaded.len(),
        failed.len()
    );

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .unwrap_or_else(|e| {
            eprintln!("绑定 {addr} 失败: {e}");
            std::process::exit(1);
        });
    axum::serve(listener, app.into_make_service())
        .await
        .unwrap_or_else(|e| {
            eprintln!("服务器错误: {e}");
            std::process::exit(1);
        });
}
