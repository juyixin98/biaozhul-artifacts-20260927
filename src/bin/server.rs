//! LZ77B HTTP server.
//!
//! Usage: lz77b-server [--listen 127.0.0.1:8080] [--store ./lz77b-store]

use std::net::SocketAddr;
use std::path::PathBuf;

fn main() {
    let mut listen: SocketAddr = "127.0.0.1:8080".parse().unwrap();
    let mut store = PathBuf::from("lz77b-store");
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--listen" => {
                listen = args
                    .next()
                    .expect("--listen needs an address")
                    .parse()
                    .expect("invalid socket address");
            }
            "--store" => store = PathBuf::from(args.next().expect("--store needs a path")),
            "-h" | "--help" => {
                println!("usage: lz77b-server [--listen ADDR] [--store DIR]");
                return;
            }
            other => panic!("unknown argument {other}"),
        }
    }

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,lz77b=debug".parse().unwrap()),
        )
        .init();

    let state = lz77b::http::handlers::AppState::new(
        lz77b::store::BlockStore::open(&store).expect("open store"),
    );

    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("runtime");
    rt.block_on(lz77b::http::routes::serve(listen, state))
        .expect("server error");
}
