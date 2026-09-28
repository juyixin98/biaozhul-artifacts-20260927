//! HTTP server entry point.
//!
//! Usage:
//!
//! ```text
//! lz77-blocks --store ./data/store --addr 127.0.0.1:8080
//! ```

use std::net::SocketAddr;
use std::process::ExitCode;

use lz77_blocks::service::app;
use lz77_blocks::store::BlockStore;

#[tokio::main]
async fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let mut store_dir = String::from("./data/store");
    let mut addr = String::from("127.0.0.1:8080");

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--store" => {
                i += 1;
                store_dir = args.get(i).cloned().unwrap_or_else(usage_exit);
            }
            "--addr" => {
                i += 1;
                addr = args.get(i).cloned().unwrap_or_else(usage_exit);
            }
            "-h" | "--help" => {
                print_help();
                return ExitCode::SUCCESS;
            }
            other => {
                eprintln!("unknown argument: {other}");
                print_help();
                return ExitCode::FAILURE;
            }
        }
        i += 1;
    }

    let parsed: SocketAddr = match addr.parse() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("invalid --addr {addr:?}: {e}");
            return ExitCode::FAILURE;
        }
    };

    let store = match BlockStore::open(&store_dir) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("fatal: cannot open store at {store_dir}: {e}");
            return ExitCode::FAILURE;
        }
    };

    let listener = match tokio::net::TcpListener::bind(parsed).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("fatal: cannot bind {parsed}: {e}");
            return ExitCode::FAILURE;
        }
    };
    eprintln!("lz77-blocks listening on http://{parsed} (store: {store_dir})");

    let server = axum::serve(listener, app(store));
    #[cfg(unix)]
    let server = server.with_graceful_shutdown(async {
        let _ = tokio::signal::ctrl_c().await;
        eprintln!("shutting down");
    });

    if let Err(e) = server.await {
        eprintln!("fatal: server error: {e}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

fn print_help() {
    eprintln!(
        "lz77-blocks — LZ77 sliding-window block compression backend\n\
         \n\
         USAGE:\n    lz77-blocks [--store <dir>] [--addr <ip:port>]\n\
         \n\
         OPTIONS:\n    --store <dir>      filesystem store root (default ./data/store)\n    --addr <ip:port>  listen address (default 127.0.0.1:8080)\n    -h, --help        show this help"
    );
}

fn usage_exit() -> String {
    print_help();
    std::process::exit(1);
}
