//! The analysis pipeline shared by the CLI and HTTP layers: parse → resolve →
//! analyze / verify, producing a fully traceable envelope.
use crate::dto::{AnalyzeRequest, Diagnostic, Envelope, Step, VerifyRequest};
use ia_lang::{parse, render_source_line, resolve};
use ia_solver::{
    analyze, AnalyzerConfig, AnalysisReport, CheckVerdict,
};
use ia_verify::{verify, VerifyConfig, VerifyReport};
use std::sync::atomic::{AtomicU64, Ordering};
use time::OffsetDateTime;

pub const SERVICE_NAME: &str = "interval-analysis-service";

static SEQ: AtomicU64 = AtomicU64::new(1);

/// Deterministic-enough local request id: `unixms-counter-nanos`. No external
/// service is involved.
pub fn new_request_id() -> String {
    let now = OffsetDateTime::now_utc();
    let millis = (now.unix_timestamp_nanos() / 1_000_000) as u128;
    let n = SEQ.fetch_add(1, Ordering::Relaxed);
    let nanos = (now.unix_timestamp_nanos() % 1_000_000) as u128;
    format!("req-{millis:012}-{n:06}-{nanos:03}")
}

fn diagnostic(source: &str, message: String, span: ia_lang::Span) -> Diagnostic {
    Diagnostic {
        message,
        start_line: span.start.line,
        start_column: span.start.column,
        end_offset: span.end.offset,
        source_excerpt: render_source_line(source, span),
    }
}

/// Result of frontend processing; either a usable program/info pair or an
/// envelope containing the diagnostics to return.
enum Frontend<'a> {
    Ok {
        program: ia_lang::Program,
        info: ia_lang::ProgramInfo,
        steps: Vec<Step>,
        request_id: String,
        _marker: std::marker::PhantomData<&'a ()>,
    },
    Err(Envelope<()>),
}

fn run_frontend(source: &str, request_id: String) -> Frontend<'_> {
    let mut steps = Vec::new();
    let parsed = parse(source);
    let program = match parsed {
        Ok(p) => {
            steps.push(Step {
                stage: "parse".to_string(),
                detail: "source parsed".to_string(),
            });
            p
        }
        Err(e) => {
            steps.push(Step {
                stage: "parse".to_string(),
                detail: "parse error".to_string(),
            });
            return Frontend::Err(Envelope {
                request_id,
                service: SERVICE_NAME,
                service_version: env!("CARGO_PKG_VERSION").to_string(),
                lang_version: ia_lang::VERSION.to_string(),
                solver_version: ia_solver::SOLVER_VERSION.to_string(),
                ok: false,
                diagnostics: vec![diagnostic(source, e.message, e.span)],
                steps,
                data: None,
            });
        }
    };
    match resolve(&program) {
        Ok(info) => {
            steps.push(Step {
                stage: "resolve".to_string(),
                detail: format!(
                    "{} input(s), {} const(s), {} array(s)",
                    info.inputs.len(),
                    info.consts.len(),
                    info.arrays.len()
                ),
            });
            Frontend::Ok {
                program,
                info,
                steps,
                request_id,
                _marker: std::marker::PhantomData,
            }
        }
        Err(errs) => {
            steps.push(Step {
                stage: "resolve".to_string(),
                detail: format!("{} validation error(s)", errs.len()),
            });
            Frontend::Err(Envelope {
                request_id,
                service: SERVICE_NAME,
                service_version: env!("CARGO_PKG_VERSION").to_string(),
                lang_version: ia_lang::VERSION.to_string(),
                solver_version: ia_solver::SOLVER_VERSION.to_string(),
                ok: false,
                diagnostics: errs
                    .into_iter()
                    .map(|e| diagnostic(source, e.message, e.span))
                    .collect(),
                steps,
                data: None,
            })
        }
    }
}

/// CLI/library entry point taking a fully-resolved analyzer configuration.
pub fn run_analyze_with(
    source: String,
    request_id: Option<String>,
    cfg: AnalyzerConfig,
) -> Envelope<AnalysisReport> {
    run_analyze(AnalyzeRequest {
        source,
        request_id,
        narrowing: Some(cfg.narrowing),
        plain_fixpoint: Some(cfg.plain_fixpoint),
        max_trace_events: Some(cfg.max_trace_events),
    })
}

/// CLI/library entry point taking a fully-resolved verifier configuration.
pub fn run_verify_with(
    source: String,
    request_id: Option<String>,
    cfg: VerifyConfig,
) -> Envelope<VerifyReport> {
    run_verify(VerifyRequest {
        source,
        request_id,
        enumeration_cap: Some(cfg.enumeration_cap),
        step_limit: Some(cfg.step_limit),
        narrowing: Some(cfg.narrowing),
    })
}

pub fn run_analyze(req: AnalyzeRequest) -> Envelope<AnalysisReport> {
    let request_id = req.request_id.clone().unwrap_or_else(new_request_id);
    log_line(&request_id, "analyze:start", &format!("{} bytes", req.source.len()));
    match run_frontend(&req.source, request_id.clone()) {
        Frontend::Ok {
            program,
            info,
            mut steps,
            request_id,
            ..
        } => {
            let cfg = AnalyzerConfig {
                narrowing: req.narrowing.unwrap_or(true),
                plain_fixpoint: req.plain_fixpoint.unwrap_or(false),
                max_trace_events: req.max_trace_events.unwrap_or(200),
                ..Default::default()
            };
            let report = analyze(&program, &info, cfg);
            steps.push(Step {
                stage: "analyze".to_string(),
                detail: format!(
                    "{} check site(s): {} safe, {} possible, {} guaranteed, {} unreachable; {} loop fixpoint(s)",
                    report.checks.len(),
                    report.counts.safe,
                    report.counts.possible_failure,
                    report.counts.guaranteed_failure,
                    report.counts.unreachable,
                    report.fixpoints.len()
                ),
            });
            log_line(
                &request_id,
                "analyze:done",
                &format!(
                    "safe={} possible={} guaranteed={} unreachable={}",
                    report.counts.safe,
                    report.counts.possible_failure,
                    report.counts.guaranteed_failure,
                    report.counts.unreachable
                ),
            );
            Envelope {
                request_id,
                service: SERVICE_NAME,
                service_version: env!("CARGO_PKG_VERSION").to_string(),
                lang_version: ia_lang::VERSION.to_string(),
                solver_version: report.solver_version.clone(),
                ok: true,
                diagnostics: Vec::new(),
                steps,
                data: Some(report),
            }
        }
        Frontend::Err(envelope) => {
            log_line(&envelope.request_id, "analyze:frontend-error", "parse/resolve");
            cast_envelope(envelope)
        }
    }
}

pub fn run_verify(req: VerifyRequest) -> Envelope<VerifyReport> {
    let request_id = req.request_id.clone().unwrap_or_else(new_request_id);
    log_line(&request_id, "verify:start", &format!("{} bytes", req.source.len()));
    match run_frontend(&req.source, request_id.clone()) {
        Frontend::Ok {
            program,
            info,
            mut steps,
            request_id,
            ..
        } => {
            let cfg = VerifyConfig {
                enumeration_cap: req.enumeration_cap.unwrap_or(200_000),
                step_limit: req.step_limit.unwrap_or(200_000),
                narrowing: req.narrowing.unwrap_or(true),
            };
            let result = verify(&req.source, &program, &info, &cfg);
            match result {
                Ok(report) => {
                    steps.push(Step {
                        stage: "verify".to_string(),
                        detail: format!(
                            "exhaustive={} combinations_run={} normal={} failed={} sound={}",
                            report.enumeration_complete,
                            report.combinations_run,
                            report.normal_runs,
                            report.failed_runs,
                            report.sound
                        ),
                    });
                    log_line(
                        &request_id,
                        "verify:done",
                        &format!(
                            "combinations={} sound={} violations={}",
                            report.combinations_run,
                            report.sound,
                            report.violations.len()
                        ),
                    );
                    Envelope {
                        request_id,
                        service: SERVICE_NAME,
                        service_version: env!("CARGO_PKG_VERSION").to_string(),
                        lang_version: ia_lang::VERSION.to_string(),
                        solver_version: ia_solver::SOLVER_VERSION.to_string(),
                        ok: true,
                        diagnostics: Vec::new(),
                        steps,
                        data: Some(report),
                    }
                }
                Err(limit) => {
                    steps.push(Step {
                        stage: "verify".to_string(),
                        detail: "enumeration cap exceeded; exhaustive verification not run"
                            .to_string(),
                    });
                    Envelope {
                        request_id,
                        service: SERVICE_NAME,
                        service_version: env!("CARGO_PKG_VERSION").to_string(),
                        lang_version: ia_lang::VERSION.to_string(),
                        solver_version: ia_solver::SOLVER_VERSION.to_string(),
                        ok: false,
                        diagnostics: vec![Diagnostic {
                            message: format!(
                                "declared input domain has {} combinations, above enumeration cap {}; \
                                 raise enumeration_cap or shrink input ranges — exhaustive check NOT run",
                                limit.combinations, limit.cap
                            ),
                            start_line: 1,
                            start_column: 1,
                            end_offset: 0,
                            source_excerpt: "(input declarations)".to_string(),
                        }],
                        steps,
                        data: None,
                    }
                }
            }
        }
        Frontend::Err(envelope) => {
            log_line(&envelope.request_id, "verify:frontend-error", "parse/resolve");
            cast_envelope(envelope)
        }
    }
}

/// Convert the frontend's `Envelope<()>` into the requested payload type.
fn cast_envelope<T: serde::Serialize>(e: Envelope<()>) -> Envelope<T> {
    Envelope {
        request_id: e.request_id,
        service: e.service,
        service_version: e.service_version,
        lang_version: e.lang_version,
        solver_version: e.solver_version,
        ok: e.ok,
        diagnostics: e.diagnostics,
        steps: e.steps,
        data: None,
    }
}

/// Structured single-line log entry on stderr: timestamp, request id and
/// stage, so requests can be correlated in external log collection.
pub fn log_line(request_id: &str, stage: &str, detail: &str) {
    let ts = OffsetDateTime::now_utc()
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap_or_default();
    eprintln!("{ts} request_id={request_id} stage={stage} {detail}");
}

/// Helper used by CLI/text renderers to summarise uncertainty distinctly.
pub fn uncertain_checks(report: &AnalysisReport) -> Vec<&ia_solver::CheckRecord> {
    report
        .checks
        .iter()
        .filter(|c| c.verdict == CheckVerdict::PossibleFailure)
        .collect()
}
