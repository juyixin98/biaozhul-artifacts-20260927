//! wtio binary: HTTP server plus a small command-line driver.
//!
//! Usage:
//!   wtio serve [--bind 127.0.0.1:8080]
//!   wtio check <request.json>            run one inclusion check, print JSON
//!   wtio verify <request.json>           check + run the independent verifier
//!
//! Exit codes: 0 included, 10 not_included (counterexample printed),
//! 20 unknown (resource bound), 1 input/state error, 2 internal error.

use std::path::PathBuf;
use std::process::ExitCode;

use wtio::compiler;
use wtio::diagnostics;
use wtio::engine;
use wtio::error::{ErrorKind, EngineError};
use wtio::input::CheckRequest;
use wtio::solver::Verdict;
use wtio::verifier;

fn usage() -> ! {
    eprintln!(
        "usage:\n  \
         wtio serve [--bind 127.0.0.1:8080]\n  \
         wtio check <request.json>\n  \
         wtio verify <request.json>"
    );
    std::process::exit(2);
}

fn read_request(path: &str) -> Result<CheckRequest, String> {
    let p = PathBuf::from(path);
    let bytes = std::fs::read(&p).map_err(|e| format!("cannot read {}: {e}", p.display()))?;
    serde_json::from_slice(&bytes).map_err(|e| format!("invalid JSON in {}: {e}", p.display()))
}

fn print_error(e: &EngineError) -> ExitCode {
    let body = serde_json::json!({
        "error": { "kind": e.kind.as_str(), "code": e.code, "message": e.message }
    });
    println!("{}", serde_json::to_string_pretty(&body).unwrap_or_default());
    match e.kind {
        ErrorKind::InputError | ErrorKind::StateConflict => ExitCode::from(1),
        ErrorKind::ResourceExhausted => ExitCode::from(20),
        ErrorKind::ComputationFailed => ExitCode::from(2),
    }
}

fn run_check(path: &str) -> ExitCode {
    let request = match read_request(path) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(1);
        }
    };
    let run_id = diagnostics::new_run_id();
    match engine::run_check_with_id(&request, run_id) {
        Ok(resp) => {
            println!("{}", serde_json::to_string_pretty(&resp).unwrap_or_default());
            match resp.verdict {
                Verdict::Included => ExitCode::from(0),
                Verdict::NotIncluded => ExitCode::from(10),
                Verdict::Unknown => ExitCode::from(20),
            }
        }
        Err(e) => print_error(&e),
    }
}

fn run_verify(path: &str) -> ExitCode {
    let request = match read_request(path) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(1);
        }
    };
    // Compile + solve ourselves so verify works even if the orchestration path
    // changes; this is the CLI equivalent of POST /api/v1/verify-replay.
    let pair = match compiler::compile(&request) {
        Ok(p) => p,
        Err(e) => return print_error(&e),
    };
    let limits = request.limits();
    let outcome = match wtio::solver::check(&pair, &limits) {
        Ok(o) => o,
        Err(e) => return print_error(&e),
    };
    match outcome.counterexample {
        Some(ce) => match verifier::verify_counterexample(&pair, &ce.implementation_replay, &ce.trace)
        {
            Ok(v) => {
                println!("{}", serde_json::to_string_pretty(&v).unwrap_or_default());
                if v.confirmed {
                    ExitCode::from(10)
                } else {
                    ExitCode::from(2)
                }
            }
            Err(e) => print_error(&e),
        },
        None => {
            println!(
                "{}",
                serde_json::json!({
                    "verdict": outcome.verdict,
                    "note": "no counterexample to verify; verdict is included or unknown",
                    "stats": outcome.stats,
                })
            );
            if outcome.verdict == Verdict::Unknown {
                ExitCode::from(20)
            } else {
                ExitCode::from(0)
            }
        }
    }
}

#[tokio::main]
async fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "wtio=info,tower_http=warn".into()),
        )
        .with_writer(std::io::stderr)
        .init();

    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("serve") => {
            let bind = match args.get(1) {
                None => "127.0.0.1:8080".to_string(),
                Some(b) if b == "--bind" => {
                    args.get(2).cloned().unwrap_or_else(|| usage())
                }
                Some(other) => other.clone(),
            };
            let addr: std::net::SocketAddr = match bind.parse() {
                Ok(a) => a,
                Err(e) => {
                    eprintln!("bad bind address '{bind}': {e}");
                    return ExitCode::from(2);
                }
            };
            match wtio::api::serve(addr).await {
                Ok(()) => ExitCode::from(0),
                Err(e) => {
                    eprintln!("server error: {e}");
                    ExitCode::from(2)
                }
            }
        }
        Some("check") => match args.get(1) {
            Some(path) => run_check(path),
            None => usage(),
        },
        Some("verify") => match args.get(1) {
            Some(path) => run_verify(path),
            None => usage(),
        },
        _ => usage(),
    }
}
