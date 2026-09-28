//! Command-line driver.
//!
//! ```text
//! rangecode encode  [--mode static|adaptive] [--bound N] [--chunk N] \
//!                   [--freq file.json | --freq-uniform] IN OUT
//! rangecode decode  [--max-symbols N] [--max-bytes N] IN OUT
//! rangecode verify  IN
//! rangecode inspect IN
//! ```
//!
//! `IN`/`OUT` are file paths (`-` means stdin/stdout).  Every command prints
//! a one-line JSON diagnostic with a generated request id.

use std::io::{Read, Write};
use std::path::PathBuf;
use std::process::ExitCode;

use rangecode::config::Config;
use rangecode::container::{decode_container, Budgets};
use rangecode::diagnostics::new_request_id;
use rangecode::ops::{self, EncodeMode};
use rangecode::table::FreqTable;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(code) => code,
    }
}

fn run(args: &[String]) -> Result<(), ExitCode> {
    let (cmd, rest) = args.split_first().ok_or_else(|| {
        eprintln!("usage: rangecode <encode|decode|verify|inspect> ...");
        ExitCode::FAILURE
    })?;
    match cmd.as_str() {
        "encode" => cmd_encode(rest),
        "decode" => cmd_decode(rest),
        "verify" => cmd_verify(rest),
        "inspect" => cmd_inspect(rest),
        "serve" => {
            let rt = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .map_err(io_fail)?;
            rt.block_on(async move {
                let cfg_file = rest
                    .iter()
                    .position(|a| a == "--config")
                    .and_then(|i| rest.get(i + 1))
                    .map(std::path::PathBuf::from);
                let cfg = Config::load(cfg_file.as_deref()).unwrap_or_else(|e| {
                    eprintln!("config error: {e}");
                    std::process::exit(2);
                });
                if let Err(e) = rangecode::server::serve(cfg).await {
                    eprintln!("server error: {e}");
                    std::process::exit(1);
                }
            });
            Ok(())
        }
        other => {
            eprintln!("unknown subcommand {other:?}");
            Err(ExitCode::FAILURE)
        }
    }
}

#[derive(Default)]
struct EncodeOpts {
    mode: Option<EncodeMode>,
    bound: Option<u32>,
    chunk: Option<u32>,
    freq_file: Option<PathBuf>,
    freq_uniform: bool,
    input: Option<PathBuf>,
    output: Option<PathBuf>,
}

fn parse_encode(rest: &[String]) -> Result<EncodeOpts, String> {
    let mut o = EncodeOpts::default();
    let mut positional = Vec::new();
    let mut i = 0;
    while i < rest.len() {
        match rest[i].as_str() {
            "--mode" => {
                i += 1;
                let v = rest.get(i).ok_or("--mode needs a value")?;
                o.mode = Some(EncodeMode::parse(v).ok_or_else(|| format!("bad mode {v}"))?);
            }
            "--bound" => {
                i += 1;
                o.bound = Some(
                    rest.get(i)
                        .ok_or("--bound needs a value")?
                        .parse()
                        .map_err(|e| format!("{e}"))?,
                );
            }
            "--chunk" => {
                i += 1;
                o.chunk = Some(
                    rest.get(i)
                        .ok_or("--chunk needs a value")?
                        .parse()
                        .map_err(|e| format!("{e}"))?,
                );
            }
            "--freq" => {
                i += 1;
                o.freq_file = Some(rest.get(i).ok_or("--freq needs a path")?.into());
            }
            "--freq-uniform" => o.freq_uniform = true,
            other if other.starts_with('-') => return Err(format!("unknown flag {other}")),
            _ => positional.push(rest[i].clone()),
        }
        i += 1;
    }
    if positional.len() != 2 {
        return Err("encode requires IN and OUT paths".to_string());
    }
    o.input = Some(positional[0].clone().into());
    o.output = Some(positional[1].clone().into());
    Ok(o)
}

fn read_path_or_dash(path: &std::path::Path) -> std::io::Result<Vec<u8>> {
    if path.as_os_str() == "-" {
        let mut buf = Vec::new();
        std::io::stdin().read_to_end(&mut buf)?;
        Ok(buf)
    } else {
        std::fs::read(path)
    }
}

fn write_path_or_dash(path: &std::path::Path, data: &[u8]) -> std::io::Result<()> {
    if path.as_os_str() == "-" {
        std::io::stdout().write_all(data)
    } else {
        std::fs::write(path, data)
    }
}

fn cmd_encode(rest: &[String]) -> Result<(), ExitCode> {
    let opts = parse_encode(rest).map_err(|e| {
        eprintln!("argument error: {e}");
        ExitCode::FAILURE
    })?;
    let cfg = Config::default();
    let bound = opts.bound.unwrap_or(cfg.frequency_bound);
    let mode = opts.mode.unwrap_or(EncodeMode::Static);
    let explicit_table = opts.freq_file.is_some() || opts.freq_uniform;
    let input_path = opts.input.clone().unwrap();
    let output_path = opts.output.clone().unwrap();
    let data = read_path_or_dash(&input_path).map_err(io_fail)?;
    let request_id = new_request_id();

    let result = if let Some(freq_path) = &opts.freq_file {
        let text = std::fs::read_to_string(freq_path).map_err(io_fail)?;
        let freqs: Vec<u32> = serde_json::from_str(&text).map_err(|e| {
            eprintln!("{request_id}: frequency file invalid JSON: {e}");
            ExitCode::FAILURE
        })?;
        let table = FreqTable::with_declared_length(&freqs, bound, 256).map_err(|e| {
            print_rejected(&request_id, "bad_table", &e.to_string(), &data);
            ExitCode::FAILURE
        })?;
        rangecode::encode_static(&data, &table, 256).map_err(|e| {
            print_rejected(&request_id, "container", &e.to_string(), &data);
            ExitCode::FAILURE
        })?
    } else if opts.freq_uniform {
        let table = rangecode::FreqTable::uniform(256, bound).unwrap();
        rangecode::encode_static(&data, &table, 256).map_err(|e| {
            print_rejected(&request_id, "container", &e.to_string(), &data);
            ExitCode::FAILURE
        })?
    } else {
        let (r, record) = ops::encode_bytes(
            &request_id,
            &data,
            mode,
            bound,
            opts.chunk.unwrap_or(cfg.chunk_target),
        );
        r.map_err(|e| {
            println!("{}", serde_json::to_string(&record).unwrap());
            let _ = e;
            ExitCode::FAILURE
        })?
        .container
    };

    write_path_or_dash(&output_path, &result).map_err(io_fail)?;
    let summary = serde_json::json!({
        "request_id": request_id,
        "decision": "accepted",
        "input_bytes": data.len(),
        "container_bytes": result.len(),
        "mode": if explicit_table { "static" } else { mode.as_str() },
    });
    println!("{}", serde_json::to_string(&summary).unwrap());
    Ok(())
}

#[derive(Default)]
struct DecodeOpts {
    max_symbols: Option<u64>,
    max_bytes: Option<u64>,
    input: Option<PathBuf>,
    output: Option<PathBuf>,
}

fn parse_decode(rest: &[String]) -> Result<DecodeOpts, String> {
    let mut o = DecodeOpts::default();
    let mut positional = Vec::new();
    let mut i = 0;
    while i < rest.len() {
        match rest[i].as_str() {
            "--max-symbols" => {
                i += 1;
                o.max_symbols = Some(
                    rest.get(i)
                        .ok_or("--max-symbols needs a value")?
                        .parse()
                        .map_err(|e: std::num::ParseIntError| e.to_string())?,
                );
            }
            "--max-bytes" => {
                i += 1;
                o.max_bytes = Some(
                    rest.get(i)
                        .ok_or("--max-bytes needs a value")?
                        .parse()
                        .map_err(|e: std::num::ParseIntError| e.to_string())?,
                );
            }
            other if other.starts_with('-') => return Err(format!("unknown flag {other}")),
            _ => positional.push(rest[i].clone()),
        }
        i += 1;
    }
    if positional.len() != 2 {
        return Err("decode requires IN and OUT paths".to_string());
    }
    o.input = Some(positional[0].clone().into());
    o.output = Some(positional[1].clone().into());
    Ok(o)
}

fn decode_budgets(o: &DecodeOpts) -> Budgets {
    let cfg = Config::default();
    Budgets {
        max_symbols: o.max_symbols.unwrap_or(cfg.max_symbols),
        max_bytes: o.max_bytes.unwrap_or(cfg.max_bytes),
        max_alphabet: cfg.max_alphabet,
    }
}

fn cmd_decode(rest: &[String]) -> Result<(), ExitCode> {
    let opts = parse_decode(rest).map_err(|e| {
        eprintln!("argument error: {e}");
        ExitCode::FAILURE
    })?;
    let input_path = opts.input.clone().unwrap();
    let output_path = opts.output.clone().unwrap();
    let blob = read_path_or_dash(&input_path).map_err(io_fail)?;
    let request_id = new_request_id();
    let budgets = decode_budgets(&opts);
    let (r, record) = ops::decode_bytes(&request_id, &blob, &budgets);
    match r {
        Ok(dr) => {
            write_path_or_dash(&output_path, &dr.data).map_err(io_fail)?;
            println!("{}", serde_json::to_string(&record).unwrap());
            Ok(())
        }
        Err(e) => {
            println!("{}", serde_json::to_string(&record).unwrap());
            let _ = e;
            Err(ExitCode::FAILURE)
        }
    }
}

fn cmd_verify(rest: &[String]) -> Result<(), ExitCode> {
    if rest.len() != 1 {
        eprintln!("usage: rangecode verify IN");
        return Err(ExitCode::FAILURE);
    }
    let blob = read_path_or_dash(rest[0].as_ref()).map_err(io_fail)?;
    let request_id = new_request_id();
    let (r, record) = ops::decode_bytes(&request_id, &blob, &Budgets::default());
    match r {
        Ok(dr) => {
            println!(
                "{}",
                serde_json::to_string(&serde_json::json!({
                    "request_id": request_id,
                    "decision": "accepted",
                    "symbols": dr.declared_symbols,
                    "chunks": dr.chunks,
                    "table_epochs": dr.table_epochs,
                }))
                .unwrap()
            );
            Ok(())
        }
        Err(_) => {
            println!("{}", serde_json::to_string(&record).unwrap());
            Err(ExitCode::FAILURE)
        }
    }
}

fn cmd_inspect(rest: &[String]) -> Result<(), ExitCode> {
    if rest.len() != 1 {
        eprintln!("usage: rangecode inspect IN");
        return Err(ExitCode::FAILURE);
    }
    let blob = read_path_or_dash(rest[0].as_ref()).map_err(io_fail)?;
    let request_id = new_request_id();
    match decode_container(&blob, &Budgets::default()) {
        Ok(p) => {
            let report = serde_json::json!({
                "request_id": request_id,
                "decision": "accepted",
                "container_bytes": blob.len(),
                "bound": p.bound,
                "alphabet": p.alphabet,
                "declared_symbols": p.declared_symbols,
                "flags": p.flags,
                "table_epochs": p.tables.len(),
                "tables": p.tables.iter().map(|t| serde_json::json!({
                    "total": t.total(),
                    "bound": t.bound(),
                    "nonzero_symbols": t.frequencies().iter().filter(|f| **f > 0).count(),
                })).collect::<Vec<_>>(),
                "chunks": p.chunks.iter().map(|c| serde_json::json!({
                    "epoch": c.epoch,
                    "symbols": c.symbols.len(),
                    "frame_bytes": c.frame_end - c.frame_start,
                })).collect::<Vec<_>>(),
            });
            println!("{}", serde_json::to_string_pretty(&report).unwrap());
            Ok(())
        }
        Err(e) => {
            print_rejected(&request_id, "container", &e.to_string(), &blob);
            Err(ExitCode::FAILURE)
        }
    }
}

fn print_rejected(request_id: &str, kind: &str, message: &str, input: &[u8]) {
    let fp = rangecode::diagnostics::PayloadFingerprint::of(input);
    let v = serde_json::json!({
        "request_id": request_id,
        "decision": "rejected",
        "error_kind": kind,
        "message": message,
        "input": fp,
    });
    println!("{}", serde_json::to_string(&v).unwrap());
}

fn io_fail(e: std::io::Error) -> ExitCode {
    eprintln!("I/O failure: {e}");
    ExitCode::FAILURE
}
