//! Binary entry point.
//!
//! Subcommands:
//! - `serve` (default): run the Axum HTTP service.
//! - `replay <spec.json> [out.jsonl]`: offline expand-and-evaluate. The
//!   spec describes one run with zero or more `segments` (ruleset + events);
//!   each segment boundary rotates the ruleset. The replay drives the
//!   incremental kernel through every step, independently re-evaluates the
//!   same trace with the set-based oracle, and prints both plus a
//!   `cross_check` field. Exit code is 2 if they disagree.
//!
//! Every run is keyed by a `run_id` (spec-provided or generated); the
//! journal kept by the kernel is emitted so a problem can be replayed from
//! the log alone.

use std::io::Write;
use std::process::ExitCode;
use std::sync::Arc;

use serde::Deserialize;
use serde_json::{json, Value};

use btmon::lang::{Event, RuleSet};
use btmon::monitor::{Limits, Monitor};
use btmon::reference;
use btmon::store::Store;

#[derive(Debug, Deserialize)]
struct ReplayFile {
    #[serde(default)]
    run_id: Option<String>,
    #[serde(default)]
    segments: Vec<Segment>,
    /// Convenience flat form: one ruleset + one trace.
    #[serde(default)]
    ruleset: Option<RuleSet>,
    #[serde(default)]
    events: Vec<Value>,
    #[serde(default)]
    closed: Option<bool>,
}

#[derive(Debug, Deserialize)]
struct Segment {
    ruleset: RuleSet,
    #[serde(default)]
    events: Vec<Value>,
}

#[tokio::main]
async fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        None | Some("serve") => serve().await,
        Some("replay") => {
            let path = args.get(2).cloned().unwrap_or_else(|| "-".to_string());
            let out = args.get(3).cloned();
            do_replay(&path, out.as_deref())
        }
        Some(other) => {
            eprintln!("unknown subcommand: {other} (expected serve|replay)");
            ExitCode::from(2)
        }
    }
}

async fn serve() -> ExitCode {
    let addr = std::env::var("BTMON_BIND").unwrap_or_else(|_| "127.0.0.1:8080".to_string());
    let store = Arc::new(Store::new());
    let app = btmon::api::router(store);
    let listener = match tokio::net::TcpListener::bind(&addr).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("failed to bind {addr}: {e}");
            return ExitCode::FAILURE;
        }
    };
    eprintln!("btmon listening on http://{addr}");
    if let Err(e) = axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
    {
        eprintln!("server error: {e}");
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}

/// Parse one event slot: either a bare event object or
/// `{"event": {...}, "step": n}`.
fn parse_slot(v: Value) -> Result<(Event, Option<i64>), String> {
    #[derive(Deserialize)]
    struct Tagged {
        event: Event,
        #[serde(default)]
        step: Option<i64>,
    }
    if let Ok(tagged) = serde_json::from_value::<Tagged>(v.clone()) {
        return Ok((tagged.event, tagged.step));
    }
    serde_json::from_value::<Event>(v)
        .map(|e| (e, None))
        .map_err(|e| format!("bad event: {e}"))
}

fn do_replay(path: &str, out: Option<&str>) -> ExitCode {
    let raw = if path == "-" {
        std::io::read_to_string(std::io::stdin()).expect("read stdin")
    } else {
        std::fs::read_to_string(path).expect("read replay spec")
    };
    let mut spec: ReplayFile = match serde_json::from_str(&raw) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("invalid replay spec: {e}");
            return ExitCode::from(2);
        }
    };

    // Normalise the flat form into segments.
    if spec.segments.is_empty() {
        if let Some(rs) = spec.ruleset.take() {
            spec.segments.push(Segment {
                ruleset: rs,
                events: std::mem::take(&mut spec.events),
            });
        } else {
            eprintln!("replay spec needs `segments` or `ruleset`+`events`");
            return ExitCode::from(2);
        }
    }

    let run_id = spec
        .run_id
        .clone()
        .unwrap_or_else(|| format!("replay-{}", uuid::Uuid::new_v4()));
    let closed = spec.closed.unwrap_or(true);

    // Drive the kernel.
    let first = &spec.segments[0].ruleset;
    let limits = Limits::replay();
    let mut monitor =
        match Monitor::new("replay".to_string(), run_id.clone(), first.clone(), limits) {
            Ok(m) => m,
            Err(e) => {
                eprintln!("ruleset invalid: {e}");
                return ExitCode::from(2);
            }
        };
    let mut kernel_steps = Vec::new();
    let mut oracle_segments: Vec<(RuleSet, Vec<(i64, Event)>)> = Vec::new();
    for (idx, seg) in spec.segments.iter().enumerate() {
        if idx > 0 {
            if let Err(e) = monitor.rotate(seg.ruleset.clone()) {
                eprintln!("rotate failed: {e}");
                return ExitCode::from(2);
            }
        }
        let mut slice: Vec<(i64, Event)> = Vec::new();
        for slot in &seg.events {
            let (ev, step) = match parse_slot(slot.clone()) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("{e}");
                    return ExitCode::from(2);
                }
            };
            // Steps are absolute across the run; rotation does not rewind the
            // timeline. Absent `step` means "next expected step".
            let report = match monitor.append(&ev, step) {
                Ok(r) => r,
                Err(e) => {
                    eprintln!("append failed at expected step {}: {e}", monitor.next_step);
                    return ExitCode::from(2);
                }
            };
            slice.push((report.step, ev));
            kernel_steps.push(report);
        }
        oracle_segments.push((seg.ruleset.clone(), slice));
    }

    // Open-state comparison must happen BEFORE the monitor is sealed.
    let open_oracle = reference::evaluate_run(&oracle_segments, false);
    let open_match = compare(&monitor, &open_oracle);
    let kernel_open_obligations = monitor.obligations.clone();
    let kernel_open_verdict = monitor.global_verdict();
    let kernel_open = json!({
        "global_verdict": kernel_open_verdict,
        "obligations": kernel_open_obligations,
        "steps": kernel_steps,
    });

    let close_report = if closed {
        Some(monitor.end().expect("close"))
    } else {
        None
    };
    let closed_oracle = reference::evaluate_run(&oracle_segments, true);
    let closed_match = compare(&monitor, &closed_oracle);
    let kernel_closed = json!({
        "global_verdict": monitor.global_verdict(),
        "obligations": monitor.obligations,
        "close": close_report,
    });

    let result = json!({
        "run_id": run_id,
        "cross_check_open": open_match,
        "cross_check_closed": closed_match,
        "kernel_open": kernel_open,
        "kernel_closed": kernel_closed,
        "oracle_open": open_oracle,
        "oracle_closed": closed_oracle,
        "journal": monitor.decisions,
        "status": monitor.status(),
        "rules": monitor.rule_outcomes(),
    });

    let line = serde_json::to_string(&result).unwrap();
    match out {
        Some(path) => {
            let mut f = std::fs::File::create(path).expect("create output");
            f.write_all(line.as_bytes()).expect("write output");
            f.write_all(b"\n").expect("write output");
        }
        None => println!("{line}"),
    }

    if open_match && closed_match {
        ExitCode::SUCCESS
    } else {
        eprintln!("cross-check failed: open={open_match} closed={closed_match}");
        ExitCode::from(2)
    }
}

/// Compare kernel obligations against the oracle map for every id.
fn compare(
    monitor: &Monitor,
    oracle: &std::collections::BTreeMap<String, reference::RefInstance>,
) -> bool {
    use btmon::lang::ObligationStatus;
    if monitor.obligations.len() != oracle.len() {
        return false;
    }
    for o in &monitor.obligations {
        let Some(r) = oracle.get(&o.id) else {
            return false;
        };
        let status_ok = matches!(
            (o.status, r.status),
            (ObligationStatus::Pending, ObligationStatus::Pending)
                | (ObligationStatus::Satisfied, ObligationStatus::Satisfied)
                | (ObligationStatus::Violated, ObligationStatus::Violated)
        );
        if !status_ok
            || o.reason != r.reason
            || o.trigger_step != r.trigger_step
            || o.window_start != r.window_start
            || o.window_end != r.window_end
            || o.satisfied_at != r.satisfied_at
            || o.failed_at != r.failed_at
        {
            return false;
        }
    }
    true
}
