//! Interval abstract interpreter for a small integer language.
//!
//! Crate layout:
//! - [`lang`]: input language — AST, lexer, recursive-descent parser
//! - [`kernel`]: solver — interval domain, abstract state, transfer functions,
//!   widening/optional-narrowing fixpoint
//! - [`concrete`]: bounded-i64 concrete interpreter (exhaustive-test reference)
//! - [`evidence`]: independent verification of analysis reports
//! - [`api`]: Axum HTTP backend
//! - [`report`] / [`config`]: serializable contract and knobs

pub mod api;
pub mod concrete;
pub mod config;
pub mod evidence;
pub mod kernel;
pub mod lang;
pub mod report;

use kernel::Analyzer;
use lang::{fnv1a64, parse};
use report::AnalysisReport;

/// Parse + validate + analyze source text. Convenience entry point used by the
/// CLI, tests and the HTTP layer.
pub fn analyze_source(
    source: &str,
    cfg: config::Config,
) -> Result<AnalysisReport, lang::LangError> {
    let program = parse(source)?;
    Analyzer::analyze(&program, cfg, fnv1a64(source))
}
