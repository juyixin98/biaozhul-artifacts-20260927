//! `mphfd` — build/query CLI and HTTP server front-end.
//!
//! Subcommands:
//! - `serve  [--config f] [--data-dir d] [--bind addr]`
//! - `build  --set name --keys file [-|@path] [--bits 0|8|16|32|64] [--seed s]
//!            [--max-attempts n] [--load-factor f] [--data-dir d]`
//! - `lookup --set name --key k [--data-dir d]`
//!
//! Keys files contain one key per line; use `--keys -` for stdin or
//! `--keys-json path` for a JSON array of strings.

use std::io::Read;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use mphf::builder::BuildConfig;
use mphf::config::ServerConfig;
use mphf::index::VerifyMode;
use mphf::persistence::Store;
use mphf::{format, Probe};
use mphf::server::{app, AppState};

#[derive(Default)]
struct CommonOpts {
    config: Option<PathBuf>,
    data_dir: Option<PathBuf>,
    bind: Option<String>,
}

fn parse_common(args: &[String]) -> Result<(CommonOpts, Vec<String>), String> {
    let mut opts = CommonOpts::default();
    let mut rest = Vec::new();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--config" => {
                opts.config = Some(PathBuf::from(next(args, &mut i, "--config")?));
            }
            "--data-dir" => {
                opts.data_dir = Some(PathBuf::from(next(args, &mut i, "--data-dir")?));
            }
            "--bind" => {
                opts.bind = Some(next(args, &mut i, "--bind")?);
            }
            other => rest.push(other.to_string()),
        }
        i += 1;
    }
    Ok((opts, rest))
}

fn next(args: &[String], i: &mut usize, flag: &str) -> Result<String, String> {
    *i += 1;
    args.get(*i)
        .cloned()
        .ok_or_else(|| format!("{flag} requires a value"))
}

struct BuildOpts {
    set: String,
    keys_path: String,
    keys_json: Option<String>,
    bits: Option<u8>,
    seed: Option<u64>,
    max_attempts: Option<u32>,
    load_factor: Option<f64>,
}

fn parse_kv<'a>(args: &'a [String], name: &str) -> Result<&'a str, String> {
    let pos = args
        .iter()
        .position(|a| a == name)
        .ok_or_else(|| format!("missing {name}"))?;
    args.get(pos + 1)
        .map(|s| s.as_str())
        .ok_or_else(|| format!("{name} requires a value"))
}

fn load_keys(o: &BuildOpts) -> Result<Vec<Vec<u8>>, String> {
    let mut keys = Vec::new();
    let raw = if o.keys_path == "-" {
        let mut s = String::new();
        std::io::stdin()
            .read_to_string(&mut s)
            .map_err(|e| format!("reading stdin: {e}"))?;
        s
    } else {
        std::fs::read_to_string(&o.keys_path)
            .map_err(|e| format!("reading {}: {e}", o.keys_path))?
    };
    for line in raw.lines() {
        if !line.is_empty() {
            keys.push(line.as_bytes().to_vec());
        }
    }
    if let Some(jp) = &o.keys_json {
        let text = std::fs::read_to_string(jp).map_err(|e| format!("reading {jp}: {e}"))?;
        let arr: Vec<String> =
            serde_json::from_str(&text).map_err(|e| format!("parsing {jp}: {e}"))?;
        for k in arr {
            keys.push(k.into_bytes());
        }
    }
    Ok(keys)
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,mphf=debug".into()),
        )
        .with_target(false)
        .init();

    let argv: Vec<String> = std::env::args().collect();
    let cmd = argv.get(1).cloned().unwrap_or_else(|| "help".into());
    let res = match cmd.as_str() {
        "serve" => cmd_serve(&argv[2..]).await,
        "build" => cmd_build(&argv[2..]).await,
        "lookup" => cmd_lookup(&argv[2..]).await,
        "help" | "--help" | "-h" => {
            print_usage();
            Ok(())
        }
        other => Err(format!("unknown subcommand {other:?}")),
    };
    if let Err(e) = res {
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}

fn print_usage() {
    println!(
        "mphfd — minimal perfect hash build/query service\n\
\n\
USAGE:\n  mphfd <COMMAND> [OPTIONS]\n\
\n\
COMMANDS:\n\
  serve     start the HTTP service\n\
  build     build and persist an index for a set\n\
  lookup    query an index on disk\n\
\n\
COMMON:\n\
  --config <file.toml>     server config (defaults shown in mphfd.example.toml)\n\
  --data-dir <dir>         index directory (default: data)\n\
  --bind <addr>            serve bind address (default: 127.0.0.1:8080)\n\
\n\
BUILD:\n\
  --set <name>             set name (stored as <data-dir>/<name>.mphf)\n\
  --keys <path|->          newline-delimited keys, '-' for stdin\n\
  --keys-json <path>       JSON array of strings (merged with --keys)\n\
  --bits <0|8|16|32|64>    verifier: 0=exact key, else fingerprint width (default 16)\n\
  --seed <u64>             base hash seed (default 1)\n\
  --max-attempts <n>       seeded retry cap (default 64)\n\
  --load-factor <f>        n/m target in (0.05,0.81] (default 0.75)\n\
\n\
LOOKUP:\n\
  --set <name> --key <k>\n"
    );
}

async fn cmd_serve(args: &[String]) -> Result<(), String> {
    let (common, _rest) = parse_common(args)?;
    let mut cfg = ServerConfig::load(common.config.as_deref()).map_err(|e| e.to_string())?;
    if let Some(d) = common.data_dir {
        cfg.data_dir = d;
    }
    if let Some(b) = common.bind {
        cfg.bind = b;
    }
    cfg.validate().map_err(|e| e.to_string())?;

    let store = Store::new(&cfg.data_dir).map_err(|e| e.to_string())?;
    let defaults = Arc::new(BuildConfig {
        load_factor: cfg.load_factor,
        base_seed: cfg.base_seed,
        max_attempts: cfg.max_attempts,
        verify: VerifyMode::parse(cfg.fingerprint_bits).map_err(|e| e.to_string())?,
        algorithm: format::ALGO_BDZ3,
    });
    let listener = tokio::net::TcpListener::bind(&cfg.bind)
        .await
        .map_err(|e| format!("bind {}: {e}", cfg.bind))?;
    let local: SocketAddr = listener.local_addr().map_err(|e| e.to_string())?;
    tracing::info!(
        "mphfd listening on http://{local} (data_dir={})",
        cfg.data_dir.display()
    );
    axum::serve(
        listener,
        app(AppState {
            store,
            defaults,
        }),
    )
    .await
    .map_err(|e| e.to_string())
}

async fn cmd_build(args: &[String]) -> Result<(), String> {
    let (common, rest) = parse_common(args)?;
    let cfg_file =
        ServerConfig::load(common.config.as_deref()).map_err(|e| e.to_string())?;
    let data_dir = common.data_dir.unwrap_or(cfg_file.data_dir);

    let o = BuildOpts {
        set: parse_kv(&rest, "--set")?.to_string(),
        keys_path: parse_kv(&rest, "--keys")?.to_string(),
        keys_json: value_of(&rest, "--keys-json").map(|s| s.to_string()),
        bits: value_of(&rest, "--bits")
            .map(|s| s.parse::<u8>())
            .transpose()
            .map_err(|e| e.to_string())?,
        seed: value_of(&rest, "--seed")
            .map(|s| s.parse::<u64>())
            .transpose()
            .map_err(|e| e.to_string())?,
        max_attempts: value_of(&rest, "--max-attempts")
            .map(|s| s.parse::<u32>())
            .transpose()
            .map_err(|e| e.to_string())?,
        load_factor: value_of(&rest, "--load-factor")
            .map(|s| s.parse::<f64>())
            .transpose()
            .map_err(|e| e.to_string())?,
    };
    let keys = load_keys(&o)?;
    let cfg = BuildConfig {
        load_factor: o.load_factor.unwrap_or(cfg_file.load_factor),
        base_seed: o.seed.unwrap_or(cfg_file.base_seed),
        max_attempts: o.max_attempts.unwrap_or(cfg_file.max_attempts),
        verify: VerifyMode::parse(o.bits.unwrap_or(cfg_file.fingerprint_bits))
            .map_err(|e| e.to_string())?,
        algorithm: format::ALGO_BDZ3,
    };

    let report = tokio::task::spawn_blocking(move || mphf::build(keys, &cfg))
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| e.to_string())?;
    let m = report.index.vertex_count();
    let (seed, attempts, distinct, duplicates, elapsed_us) = (
        report.seed,
        report.attempts,
        report.distinct_keys,
        report.duplicates_dropped,
        report.elapsed_us,
    );
    let store = Store::new(&data_dir).map_err(|e| e.to_string())?;
    store
        .save(&o.set, report.index)
        .await
        .map_err(|e| e.to_string())?;
    let path = data_dir.join(format!("{}.mphf", o.set));
    let bytes = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    println!(
        "built set {set:?}: n={distinct} m={m} seed={seed} attempts={attempts} \
duplicates_dropped={duplicates} elapsed_us={elapsed_us}",
        set = o.set
    );
    println!("persisted {} ({bytes} bytes on disk)", path.display());
    Ok(())
}

fn value_of<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
    args.iter()
        .position(|a| a == name)
        .and_then(|p| args.get(p + 1).map(|s| s.as_str()))
}

async fn cmd_lookup(args: &[String]) -> Result<(), String> {
    let (common, rest) = parse_common(args)?;
    let cfg_file =
        ServerConfig::load(common.config.as_deref()).map_err(|e| e.to_string())?;
    let data_dir = common.data_dir.unwrap_or(cfg_file.data_dir);
    let set = parse_kv(&rest, "--set")?;
    let key = parse_kv(&rest, "--key")?;

    let path = data_dir.join(format!("{set}.mphf"));
    let idx = format::load_from_path(&path).map_err(|e| e.to_string())?;
    match idx.probe(key.as_bytes()) {
        Probe::Member { slot } => {
            println!("ACCEPT member slot={slot}");
            Ok(())
        }
        Probe::Rejected { reason, slot } => {
            println!(
                "REJECT non-member reason={} candidate_slot={:?}",
                reason.as_str(),
                slot
            );
            std::process::exit(2)
        }
        Probe::Inconclusive { reason } => Err(format!("UNDETERMINED {reason}")),
    }
}
