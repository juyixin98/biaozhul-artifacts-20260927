//! CLI and HTTP server entry point.
//!
//! Subcommands:
//!
//! * `serve [--config path]` — run the Axum verification API.
//! * `encode [--adaptive] [--chunk N] [--symbols N] [--freqs f0,f1,…]
//!    <input> <output>` — encode a raw file into an RC01 container.
//! * `decode <input> <output>` — decode an RC01 container to raw bytes.
//! * `verify <input>` — validate a container, print the verdict diagnostic.
//!
//! If no command is given, `serve` runs with defaults.

use range_codec::config::Config;
use range_codec::container::{
    decode, encode_adaptive, encode_empty, encode_static, DecodeBudget, ModelMode, StaticChunk,
};
use range_codec::diagnostics::{log_diagnostic, Diagnostic, RequestId};
use range_codec::persist::Store;
use range_codec::{api, CodecError};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;

#[derive(Debug)]
struct Args {
    command: String,
    positional: Vec<String>,
    config: Option<PathBuf>,
    adaptive: bool,
    chunk: Option<usize>,
    symbols: Option<u16>,
    freqs: Option<Vec<u32>>,
}

fn parse_args(argv: Vec<String>) -> Result<Args, String> {
    let mut args = Args {
        command: "serve".into(),
        positional: Vec::new(),
        config: None,
        adaptive: false,
        chunk: None,
        symbols: None,
        freqs: None,
    };
    let mut iter = argv.into_iter();
    // argv[0]
    let _ = iter.next();
    if let Some(cmd) = iter.next() {
        if !cmd.starts_with('-') {
            args.command = cmd;
        } else {
            args.positional.push(cmd);
        }
    }
    while let Some(a) = iter.next() {
        match a.as_str() {
            "--config" => {
                args.config = Some(PathBuf::from(iter.next().ok_or("--config needs value")?))
            }
            "--adaptive" => args.adaptive = true,
            "--chunk" => {
                args.chunk = Some(
                    iter.next()
                        .ok_or("--chunk needs value")?
                        .parse()
                        .map_err(|_| "bad --chunk")?,
                );
            }
            "--symbols" => {
                args.symbols = Some(
                    iter.next()
                        .ok_or("--symbols needs value")?
                        .parse()
                        .map_err(|_| "bad --symbols")?,
                );
            }
            "--freqs" => {
                let raw = iter.next().ok_or("--freqs needs value")?;
                let parsed: std::result::Result<Vec<u32>, _> =
                    raw.split(',').map(|s| s.trim().parse::<u32>()).collect();
                args.freqs = Some(parsed.map_err(|_| "bad --freqs list")?);
            }
            other => args.positional.push(other.to_string()),
        }
    }
    Ok(args)
}

fn print_diagnostic(input: &[u8], result: &std::result::Result<(), CodecError>) -> i32 {
    let rid = RequestId::new();
    let d = Diagnostic::for_result(&rid, input, result, vec![]);
    println!("{}", serde_json::to_string_pretty(&d).unwrap());
    match result {
        Ok(()) => 0,
        Err(_) => 2,
    }
}

fn cmd_encode(args: &Args) -> Result<(), String> {
    let input = args.positional.first().ok_or("usage: encode <in> <out>")?;
    let output = args.positional.get(1).ok_or("usage: encode <in> <out>")?;
    let raw = std::fs::read(input).map_err(|e| format!("read {input}: {e}"))?;

    let num_symbols = args.symbols.unwrap_or(256);
    let data: Vec<u8> = if raw.is_empty() {
        let mode = if args.adaptive {
            ModelMode::Adaptive
        } else {
            ModelMode::Static
        };
        encode_empty(mode, num_symbols).map_err(|e| e.to_string())?
    } else if args.adaptive {
        let size = args.chunk.unwrap_or(4096);
        let parts = range_codec::container::split_chunks(&raw, size);
        encode_adaptive(num_symbols, &parts).map_err(|e| e.to_string())?
    } else {
        let freqs = args
            .freqs
            .clone()
            .unwrap_or_else(|| vec![1u32; num_symbols as usize]);
        if freqs.len() != num_symbols as usize {
            return Err(format!(
                "--freqs has {} entries but --symbols is {num_symbols}",
                freqs.len()
            ));
        }
        let size = args.chunk.unwrap_or(4096);
        let parts = range_codec::container::split_chunks(&raw, size);
        let chunks: Vec<StaticChunk> = parts
            .into_iter()
            .map(|symbols| StaticChunk {
                freqs: freqs.clone(),
                symbols,
            })
            .collect();
        encode_static(&chunks).map_err(|e| e.to_string())?
    };

    std::fs::write(output, &data).map_err(|e| format!("write {output}: {e}"))?;
    eprintln!(
        "encoded {} input byte(s) -> {} container byte(s) ({})",
        raw.len(),
        data.len(),
        output
    );
    Ok(())
}

fn cmd_decode(args: &Args) -> i32 {
    let Some(input) = args.positional.first() else {
        eprintln!("usage: decode <in> <out>");
        return 2;
    };
    let Some(output) = args.positional.get(1) else {
        eprintln!("usage: decode <in> <out>");
        return 2;
    };
    let raw = match std::fs::read(input) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("read {input}: {e}");
            return 2;
        }
    };
    let result = decode(&raw, &DecodeBudget::default());
    match result {
        Ok(d) => {
            if let Err(e) = std::fs::write(output, &d.symbols) {
                eprintln!("write {output}: {e}");
                return 2;
            }
            eprintln!(
                "decoded {} symbol(s) from {} chunk(s) -> {}",
                d.symbols.len(),
                d.chunks,
                output
            );
            0
        }
        Err(e) => {
            let rid = RequestId::new();
            let d = Diagnostic::from_error(&rid, &raw, &e, vec![]);
            println!("{}", serde_json::to_string_pretty(&d).unwrap());
            2
        }
    }
}

fn cmd_verify(args: &Args) -> i32 {
    let Some(input) = args.positional.first() else {
        eprintln!("usage: verify <in>");
        return 2;
    };
    let raw = match std::fs::read(input) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("read {input}: {e}");
            return 2;
        }
    };
    let result = decode(&raw, &DecodeBudget::default()).map(|_| ());
    print_diagnostic(&raw, &result)
}

async fn serve(config: Config) -> Result<(), Box<dyn std::error::Error>> {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_new(&config.log_level).unwrap_or_else(|_| EnvFilter::new("info"));
    fmt().with_env_filter(filter).init();

    let store = Store::new(&config.data_dir)?;
    let state = api::AppState {
        config: Arc::new(config.clone()),
        store: Arc::new(store),
    };
    let listener = tokio::net::TcpListener::bind(&config.listen).await?;
    tracing::info!(addr = %config.listen, data_dir = %config.data_dir.display(), "range-codec listening");
    axum::serve(listener, api::router(state)).await?;
    Ok(())
}

fn main() -> ExitCode {
    let args = match parse_args(std::env::args().collect()) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("argument error: {e}");
            return ExitCode::from(2);
        }
    };

    match args.command.as_str() {
        "serve" => {
            let config = match Config::load(args.config.as_deref()) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("config error: {e}");
                    return ExitCode::from(2);
                }
            };
            let rt = match tokio::runtime::Runtime::new() {
                Ok(rt) => rt,
                Err(e) => {
                    eprintln!("runtime error: {e}");
                    return ExitCode::from(1);
                }
            };
            match rt.block_on(serve(config)) {
                Ok(()) => ExitCode::SUCCESS,
                Err(e) => {
                    eprintln!("server error: {e}");
                    ExitCode::from(1)
                }
            }
        }
        "encode" => match cmd_encode(&args) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("encode error: {e}");
                ExitCode::from(2)
            }
        },
        "decode" => ExitCode::from(cmd_decode(&args) as u8),
        "verify" => ExitCode::from(cmd_verify(&args) as u8),
        other => {
            eprintln!("unknown command {other:?}; expected serve|encode|decode|verify");
            ExitCode::from(2)
        }
    }
}

#[allow(dead_code)]
fn ensure_logging_linked() {
    let rid = RequestId::new();
    let d = Diagnostic::from_error(&rid, &[], &CodecError::DecoderExhausted, vec![]);
    log_diagnostic(&d, &[], true);
}
