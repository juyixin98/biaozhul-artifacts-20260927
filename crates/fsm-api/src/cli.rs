//! Command-line interface, allowing fully local verification without the
//! HTTP server:
//!
//! ```text
//! fsm-check serve
//! fsm-check check  [--fixture NAME | --file PATH | --json PATH]
//!                 [--ag EXPR]... [--ef EXPR]...
//!                 [--no-deadlock] [--max-states N]
//! fsm-check verify SPEC_FILE EVIDENCE_JSON
//! fsm-check fixtures
//! ```

use std::sync::Arc;

use anyhow::{anyhow, Context, Result};
use serde_json::json;

use fsm_lang::eval::build_system;
use fsm_lang::parser::parse_system;

use crate::config::Config;
use crate::routes::app;
use crate::service::ServiceState;
use crate::types::{prepare, CheckRequest, PropertyInput};

pub struct ParsedArgs {
    pub fixture: Option<String>,
    pub file: Option<String>,
    pub json_file: Option<String>,
    pub ag: Vec<String>,
    pub ef: Vec<String>,
    pub no_deadlock: bool,
    pub max_states: Option<u64>,
}

pub async fn run(args: Vec<String>) -> Result<()> {
    let (cmd, rest) = match args.split_first() {
        Some((c, r)) => (c.as_str(), r.to_vec()),
        None => {
            print_usage();
            return Ok(());
        }
    };
    match cmd {
        "serve" => serve(rest).await,
        "check" => check(rest),
        "verify" => verify(rest),
        "fixtures" => {
            println!(
                "{}",
                serde_json::to_string_pretty(&json!([
                    "mutex_safe",
                    "mutex_bad",
                    "counter",
                    "counter_deadlock",
                    "no_init",
                    "big_counter",
                    "swap"
                ]))
                .unwrap()
            );
            Ok(())
        }
        "-h" | "--help" | "help" => {
            print_usage();
            Ok(())
        }
        other => Err(anyhow!("unknown subcommand '{other}'")),
    }
}

fn print_usage() {
    println!(
        "explicit FSM model checker\n\n\
         USAGE:\n  \
         fsm-check serve\n  \
         fsm-check check [--fixture NAME|--file PATH|--json PATH] [--ag EXPR]... [--ef EXPR]... [--no-deadlock] [--max-states N]\n  \
         fsm-check verify SPEC_TEXT_FILE EVIDENCE_JSON\n  \
         fsm-check fixtures\n"
    );
}

async fn serve(rest: Vec<String>) -> Result<()> {
    let config = Config::from_env();
    let mut bind = config.bind.clone();
    let mut iter = rest.iter();
    while let Some(a) = iter.next() {
        if a == "--bind" {
            bind = iter
                .next()
                .cloned()
                .ok_or_else(|| anyhow!("--bind requires an argument"))?;
        }
    }
    let state = Arc::new(ServiceState {
        config: Config {
            bind: bind.clone(),
            ..config
        },
    });
    let listener = tokio::net::TcpListener::bind(&bind)
        .await
        .with_context(|| format!("failed to bind {bind}"))?;
    tracing::info!("fsm-check HTTP API listening on {bind}");
    axum::serve(listener, app(state))
        .await
        .context("server error")?;
    Ok(())
}

fn parse_check_args(rest: &[String]) -> Result<ParsedArgs> {
    let mut p = ParsedArgs {
        fixture: None,
        file: None,
        json_file: None,
        ag: Vec::new(),
        ef: Vec::new(),
        no_deadlock: false,
        max_states: None,
    };
    let mut i = 0;
    while i < rest.len() {
        let a = &rest[i];
        let take = |i: &mut usize| -> Result<String> {
            *i += 1;
            rest.get(*i)
                .cloned()
                .ok_or_else(|| anyhow!("{a} requires an argument"))
        };
        match a.as_str() {
            "--fixture" => p.fixture = Some(take(&mut i)?),
            "--file" => p.file = Some(take(&mut i)?),
            "--json" => p.json_file = Some(take(&mut i)?),
            "--ag" => p.ag.push(take(&mut i)?),
            "--ef" => p.ef.push(take(&mut i)?),
            "--no-deadlock" => p.no_deadlock = true,
            "--max-states" => {
                let v = take(&mut i)?;
                p.max_states = Some(v.parse().context("--max-states must be a number")?);
            }
            other => return Err(anyhow!("unknown check argument '{other}'")),
        }
        i += 1;
    }
    Ok(p)
}

fn check(rest: Vec<String>) -> Result<()> {
    let p = parse_check_args(&rest)?;
    let mut properties = Vec::new();
    for (i, e) in p.ag.iter().enumerate() {
        properties.push(PropertyInput {
            name: format!("ag_{i}"),
            kind: "ag".into(),
            expr: Some(e.clone()),
        });
    }
    for (i, e) in p.ef.iter().enumerate() {
        properties.push(PropertyInput {
            name: format!("ef_{i}"),
            kind: "ef".into(),
            expr: Some(e.clone()),
        });
    }

    let spec_text = match (&p.file, &p.fixture) {
        (Some(path), None) => {
            Some(std::fs::read_to_string(path).with_context(|| format!("read {path}"))?)
        }
        (None, Some(name)) => Some(resolve_fixture(name)?.to_string()),
        (None, None) => None,
        (Some(_), Some(_)) => return Err(anyhow!("use only one of --fixture/--file/--json")),
    };
    let spec_json = match &p.json_file {
        Some(path) => Some(serde_json::from_str(
            &std::fs::read_to_string(path).with_context(|| format!("read {path}"))?,
        )?),
        None => None,
    };

    let req = CheckRequest {
        fixture: None,
        spec_text,
        spec_json,
        properties,
        max_states: p.max_states,
        check_deadlock: Some(!p.no_deadlock),
    };
    let config = Config::default();
    let resolved = prepare(req, config.default_max_states)
        .map_err(|f| anyhow!("specification failed: [{}] {}", f.category, f.detail))?;
    let outcome = fsm_core::run_check(&resolved.system, &resolved.queries, resolved.options);
    println!("{}", serde_json::to_string_pretty(&outcome)?);
    if outcome.error.is_some() {
        std::process::exit(2);
    }
    if outcome
        .properties
        .iter()
        .any(|p| matches!(p.conclusion, fsm_core::Conclusion::Violated))
    {
        std::process::exit(1);
    }
    Ok(())
}

fn verify(rest: Vec<String>) -> Result<()> {
    let (spec_path, ev_path) = match rest.as_slice() {
        [a, b] => (a.clone(), b.clone()),
        _ => {
            return Err(anyhow!(
                "usage: fsm-check verify SPEC_TEXT_FILE EVIDENCE_JSON"
            ))
        }
    };
    let spec = std::fs::read_to_string(&spec_path).with_context(|| format!("read {spec_path}"))?;
    let raw = parse_system(&spec).map_err(|e| anyhow!("spec parse: {e}"))?;
    let system = build_system(raw).map_err(|e| anyhow!("spec build: {e}"))?;
    let ev: fsm_verify::EvidenceInput =
        serde_json::from_str(&std::fs::read_to_string(&ev_path)?).context("evidence JSON")?;
    let report = fsm_verify::verify(&system, &ev);
    println!("{}", serde_json::to_string_pretty(&report)?);
    if !report.accepted {
        std::process::exit(1);
    }
    Ok(())
}

fn resolve_fixture(name: &str) -> Result<&'static str> {
    Ok(match name {
        "mutex_safe" => fsm_fixtures::mutex_safe(),
        "mutex_bad" => fsm_fixtures::mutex_bad(),
        "counter" => fsm_fixtures::counter(),
        "counter_deadlock" => fsm_fixtures::counter_deadlock(),
        "no_init" => fsm_fixtures::no_init(),
        "big_counter" => fsm_fixtures::big_counter(),
        "swap" => fsm_fixtures::swap(),
        other => return Err(anyhow!("unknown fixture '{other}'")),
    })
}
