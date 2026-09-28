//! `hbs` command line: run the server, generate fixtures, and inspect /
//! verify set files.

use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;

use hbs_config::Config;
use hbs_server::AppState;
use hbs_store::FileStore;
use hbs_testkit::fixtures::generate_fixtures;
use tracing::{error, info};

fn usage() -> &'static str {
    "hbs - hierarchical bitmap set toolkit\n\n\
     USAGE:\n  \
       hbs <COMMAND> [OPTIONS]\n\n\
     COMMANDS:\n  \
       serve --config <path>          Run the verification HTTP API\n  \
       gen-fixtures --out <dir>      Write deterministic synthetic fixtures\n  \
       verify --data-dir <dir> <name>   Validate one stored set file\n  \
       inspect --data-dir <dir> <name>  Print cardinality / chunk statistics\n\
     \n\
     ENVIRONMENT:\n  \
       HBS_DATA_DIR, HBS_BIND_ADDR, HBS_MAX_REQUEST_BYTES,\n  \
       HBS_SYNC_WRITES, HBS_LOG_LEVEL override file configuration.\n"
}

fn args() -> Vec<String> {
    std::env::args().skip(1).collect()
}

fn flag_value(args: &[String], flag: &str) -> Option<String> {
    args.iter()
        .position(|a| a == flag)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

#[tokio::main]
async fn main() -> ExitCode {
    let argv = args();
    let cmd = match argv.first() {
        Some(c) => c.as_str(),
        None => {
            eprint!("{}", usage());
            return ExitCode::from(2);
        }
    };

    match cmd {
        "serve" => serve(flag_value(&argv, "--config")).await,
        "gen-fixtures" => gen_fixtures(flag_value(&argv, "--out")),
        "verify" => verify(&argv),
        "inspect" => inspect(&argv),
        "-h" | "--help" | "help" => {
            print!("{}", usage());
            ExitCode::SUCCESS
        }
        other => {
            eprintln!("unknown command: {other}\n");
            eprint!("{}", usage());
            ExitCode::from(2)
        }
    }
}

fn init_tracing(level: Option<&str>) {
    let env_level = std::env::var("HBS_LOG_LEVEL").ok();
    let filter = level.or(env_level.as_deref()).unwrap_or("info");
    let env_filter = tracing_subscriber::EnvFilter::try_new(filter)
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
    let _ = tracing_subscriber::fmt()
        .with_env_filter(env_filter)
        .try_init();
}

async fn serve(config_path: Option<String>) -> ExitCode {
    let config = match Config::load(config_path.unwrap_or_else(|| "hbs.conf".to_string())) {
        Ok(c) => c,
        Err(e) => {
            error!(error = %e, "failed to load configuration");
            eprintln!("configuration error: {e}");
            return ExitCode::from(2);
        }
    };
    init_tracing(Some(&config.log_level));

    let store = match FileStore::open(&config.data_dir, config.sync_writes) {
        Ok(s) => s,
        Err(e) => {
            error!(error = %e, dir = %config.data_dir, "cannot open data directory");
            return ExitCode::FAILURE;
        }
    };
    let state = AppState {
        store: Arc::new(store),
        config: Arc::new(config.clone()),
    };
    let app = hbs_server::build_app(state);
    let listener = match tokio::net::TcpListener::bind(&config.bind_addr).await {
        Ok(l) => l,
        Err(e) => {
            error!(error = %e, addr = %config.bind_addr, "cannot bind");
            return ExitCode::FAILURE;
        }
    };
    info!(addr = %config.bind_addr, data_dir = %config.data_dir, "hbs server listening");
    if let Err(e) = axum::serve(listener, app).await {
        error!(error = %e, "server terminated");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

fn gen_fixtures(out: Option<String>) -> ExitCode {
    let Some(out) = out else {
        eprintln!("gen-fixtures requires --out <dir>");
        return ExitCode::from(2);
    };
    match generate_fixtures(&out) {
        Ok(metas) => {
            for m in &metas {
                println!(
                    "{:24} card={:>7} chunks={:>3} arrays={:>3} bitmaps={:>3}",
                    m.label, m.cardinality, m.chunks, m.array_containers, m.bitmap_containers
                );
            }
            println!(
                "wrote {} fixture scenarios to {}",
                metas.len(),
                PathBuf::from(&out).display()
            );
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("fixture generation failed: {e}");
            ExitCode::FAILURE
        }
    }
}

/// Flags whose immediately following argument is a value, not a positional.
const VALUE_FLAGS: &[&str] = &["--config", "--out", "--data-dir"];

fn positional(args: &[String]) -> Option<String> {
    let mut iter = args.iter().skip(1);
    while let Some(a) = iter.next() {
        if VALUE_FLAGS.contains(&a.as_str()) {
            // Skip the flag's value too.
            iter.next();
        } else if !a.starts_with("--") {
            return Some(a.clone());
        }
    }
    None
}

fn verify(argv: &[String]) -> ExitCode {
    let data_dir = flag_value(argv, "--data-dir").unwrap_or_else(|| "./data".to_string());
    let Some(name) = positional(argv) else {
        eprintln!("verify requires a set name");
        return ExitCode::from(2);
    };
    let store = match FileStore::open(&data_dir, false) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("cannot open {data_dir}: {e}");
            return ExitCode::FAILURE;
        }
    };
    match store.verify(&name) {
        Ok(d) => {
            println!("OK {name}: valid format v{}", hbs_format::FORMAT_VERSION);
            println!(
                "  chunks={} array={} bitmap={} bytes={} cardinality={}",
                d.num_chunks,
                d.array_containers,
                d.bitmap_containers,
                d.encoded_len,
                d.set.len()
            );
            ExitCode::SUCCESS
        }
        Err(hbs_store::StoreError::NotFound(_)) => {
            eprintln!("set {name:?} not found in {data_dir}");
            ExitCode::from(3)
        }
        Err(hbs_store::StoreError::Corrupt(e)) => {
            // Distinct exit code for corruption so scripts can classify it.
            eprintln!("CORRUPT {name}: {e}");
            ExitCode::from(4)
        }
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn inspect(argv: &[String]) -> ExitCode {
    let data_dir = flag_value(argv, "--data-dir").unwrap_or_else(|| "./data".to_string());
    let Some(name) = positional(argv) else {
        eprintln!("inspect requires a set name");
        return ExitCode::from(2);
    };
    let store = match FileStore::open(&data_dir, false) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("cannot open {data_dir}: {e}");
            return ExitCode::FAILURE;
        }
    };
    match store.load(&name) {
        Ok(set) => {
            let stats = set.stats();
            println!("set {name}");
            println!("  cardinality: {}", set.len());
            println!("  chunks:      {}", stats.chunks);
            println!("  arrays:      {}", stats.array_containers);
            println!("  bitmaps:     {}", stats.bitmap_containers);
            println!(
                "  range:       {}..={}",
                set.min().unwrap_or(0),
                set.max().unwrap_or(0)
            );
            ExitCode::SUCCESS
        }
        Err(hbs_store::StoreError::NotFound(_)) => {
            eprintln!("set {name:?} not found in {data_dir}");
            ExitCode::from(3)
        }
        Err(hbs_store::StoreError::Corrupt(e)) => {
            eprintln!("CORRUPT {name}: {e}");
            ExitCode::from(4)
        }
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}
