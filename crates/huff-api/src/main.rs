//! `huff` — canonical-Huffman compression backend CLI.
//!
//! Subcommands:
//! * `serve`                       run the Axum HTTP service
//! * `encode  <in> <out.hfc>`      compress a file (block size from config)
//! * `decode  <in.hfc> <out>`      verify + decompress
//! * `validate <in.hfc>`           print the multi-step validation report
//!
//! Exit codes: 0 success; 2 codec/validation failure (error code printed);
//! 1 usage/IO failure.

use std::path::Path;
use std::process::ExitCode;

use huff_api::config::Config;
use huff_api::logging;
use huff_api::server::{self, request_id};
use huff_core::container::{decode_container, encode_container};
use huff_core::error::HuffError;
use huff_core::report;

fn print_usage() {
    eprintln!(
        "usage:\n  huff serve\n  huff encode <input> <output.hfc>\n  huff decode <input.hfc> <output>\n  huff validate <input.hfc>"
    );
}

fn read_bytes(path: &Path) -> Result<Vec<u8>, String> {
    std::fs::read(path).map_err(|e| format!("cannot read {}: {e}", path.display()))
}

fn write_bytes(path: &Path, data: &[u8]) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("cannot create {}: {e}", parent.display()))?;
        }
    }
    std::fs::write(path, data).map_err(|e| format!("cannot write {}: {e}", path.display()))
}

fn run() -> Result<(), String> {
    let config = Config::from_env().map_err(|e| e.to_string())?;
    logging::init(config.log_dir.clone());

    let mut args = std::env::args().skip(1);
    let cmd = args.next().ok_or_else(|| {
        print_usage();
        "missing subcommand".to_string()
    })?;

    match cmd.as_str() {
        "serve" => {
            let rt = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .map_err(|e| format!("tokio runtime: {e}"))?;
            rt.block_on(server::serve(config))
                .map_err(|e| format!("server error: {e}"))
        }
        "encode" => {
            let input = args.next().ok_or("encode needs <input> <output>")?;
            let output = args.next().ok_or("encode needs <input> <output>")?;
            let data = read_bytes(Path::new(&input))?;
            let run_id = request_id();
            logging::record(
                &run_id,
                &format!("cli encode start: input={input} input_bytes={} block_size={}", data.len(), config.block_size),
            );
            let container = encode_container(&data, config.block_size)
                .map_err(|e: HuffError| format!("encode failed: {} ({})", e.code(), e))?;
            write_bytes(Path::new(&output), &container)?;
            logging::record(
                &run_id,
                &format!("cli encode done: output={output} container_bytes={} verdict=PASS", container.len()),
            );
            println!(
                "encoded {input} -> {output}: {} -> {} bytes (run {run_id})",
                data.len(),
                container.len()
            );
            Ok(())
        }
        "decode" => {
            let input = args.next().ok_or("decode needs <input.hfc> <output>")?;
            let output = args.next().ok_or("decode needs <input.hfc> <output>")?;
            let container = read_bytes(Path::new(&input))?;
            let run_id = request_id();
            logging::record(
                &run_id,
                &format!("cli decode start: input={input} container_bytes={}", container.len()),
            );
            let data = decode_container(&container)
                .map_err(|e: HuffError| format!("decode failed: {} ({})", e.code(), e))?;
            write_bytes(Path::new(&output), &data)?;
            logging::record(
                &run_id,
                &format!("cli decode done: output={output} original_bytes={} verdict=PASS", data.len()),
            );
            println!("decoded {input} -> {output}: {} bytes (run {run_id})", data.len());
            Ok(())
        }
        "validate" => {
            let input = args.next().ok_or("validate needs <input.hfc>")?;
            let container = read_bytes(Path::new(&input))?;
            let run_id = request_id();
            logging::record(
                &run_id,
                &format!(
                    "cli validate start: input={input} container_bytes={} format_version=1",
                    container.len()
                ),
            );
            let r = report::validate(&container);
            for c in &r.checks {
                let basis = if let Some(code) = &c.error_code {
                    format!("{} [{}]", c.detail, code)
                } else {
                    c.detail.clone()
                };
                println!("{:<14} {:<4} {}", c.name, c.verdict.to_uppercase(), basis);
                logging::record(
                    &run_id,
                    &format!("cli validate check={} verdict={} basis={basis}", c.name, c.verdict),
                );
            }
            let overall = if r.ok { "PASS" } else { "FAIL" };
            println!("overall        {overall} (run {run_id})");
            logging::record(&run_id, &format!("cli validate verdict={overall}"));
            if !r.ok {
                return Err(format!(
                    "validation failed: {}",
                    r.first_error.unwrap_or_else(|| "UNKNOWN".to_string())
                ));
            }
            Ok(())
        }
        other => {
            print_usage();
            Err(format!("unknown subcommand: {other}"))
        }
    }
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::from(0),
        Err(message) => {
            eprintln!("error: {message}");
            // Distinguish codec/validation failures (2) from usage/IO (1).
            if message.contains("failed:") || message.starts_with("validation failed:") {
                ExitCode::from(2)
            } else {
                ExitCode::from(1)
            }
        }
    }
}
