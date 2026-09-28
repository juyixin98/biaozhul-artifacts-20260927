//! Solver backend: the [`SmtSolver`] abstraction and the Z3 command-line backend.
//!
//! The service deliberately drives the **mature Z3 theorem prover** through its
//! documented SMT-LIB 2 interface (`z3 -smt2 -in`), avoiding a dependency on a
//! matching `libz3` at build time.  One query = one fresh process with a complete
//! formula, which keeps the backend stateless and easy to audit.
//!
//! Unsatisfiability is treated conservatively: only an explicit `unsat` answer prunes
//! paths; `unknown`, timeouts, parser errors and missing binaries all surface as
//! [`CheckStatus::Unknown`] rather than being silently absorbed.

use std::collections::BTreeMap;
use std::process::{Command, Stdio};
use std::time::Duration;

use se_lang::Width;
use thiserror::Error;

use crate::smt;
use crate::term::Term;

/// Result status of one satisfiability query.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CheckStatus {
    Sat,
    Unsat,
    /// Solver said `unknown`, timed out, or the backend could not obtain a verdict.
    Unknown,
}

/// A concrete assignment to the queried input variables.
pub type Model = BTreeMap<String, u64>;

#[derive(Clone, Debug)]
pub struct CheckResult {
    pub status: CheckStatus,
    pub model: Option<Model>,
    pub reason: Option<String>,
    pub solver: String,
    pub solver_version: String,
    /// Number of milliseconds the solver reported working (best effort).
    pub elapsed_ms: u128,
}

#[derive(Debug, Error)]
pub enum SolverError {
    #[error("SMT sort error: {0}")]
    Sort(#[from] smt::SortError),
    #[error("failed to launch solver '{bin}': {source}")]
    Spawn {
        bin: String,
        #[source]
        source: std::io::Error,
    },
    #[error("solver io error: {0}")]
    Io(#[from] std::io::Error),
}

/// Minimal interface the symbolic engine depends on. It is a trait so the engine can
/// be tested with deterministic stubs while production wiring uses [`Z3Cli`].
pub trait SmtSolver {
    /// Check satisfiability of the conjunction of `assumptions`; on [`CheckStatus::Sat`]
    /// return a model for the declared `inputs` when one could be parsed.
    fn check(
        &self,
        width: Width,
        inputs: &[String],
        assumptions: &[Term],
    ) -> Result<CheckResult, SolverError>;

    fn name(&self) -> &str;
    fn version(&self) -> &str;
}

/// Z3 command-line backend configuration.
#[derive(Clone, Debug)]
pub struct Z3Cli {
    /// Path to the z3 executable.
    pub bin: String,
    /// Soft timeout passed to Z3 via `(set-option :timeout ...)`, milliseconds.
    pub timeout_ms: u32,
    /// Hard wall-clock budget for the child process.
    pub wall_timeout: Duration,
    version: String,
}

impl Z3Cli {
    /// Create a backend and probe `z3 --version` once so the version is attached to
    /// every report.  A missing binary is recorded rather than panicked on; queries
    /// will then return `Unknown`.
    pub fn new(bin: impl Into<String>, timeout_ms: u32) -> Self {
        let bin = bin.into();
        let version = probe_version(&bin).unwrap_or_else(|_| "unavailable".to_string());
        Z3Cli {
            bin,
            timeout_ms,
            wall_timeout: Duration::from_millis(timeout_ms as u64 + 1_500),
            version,
        }
    }

    pub fn available(&self) -> bool {
        self.version != "unavailable"
    }

    fn run_query(&self, script: &str) -> (CheckStatus, Option<Model>, Option<String>, u128) {
        let started = std::time::Instant::now();
        let mut child = match Command::new(&self.bin)
            .arg("-smt2")
            .arg("-in")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
        {
            Ok(c) => c,
            Err(e) => {
                return (
                    CheckStatus::Unknown,
                    None,
                    Some(format!("spawn failed: {e}")),
                    0,
                )
            }
        };

        {
            use std::io::Write;
            let mut stdin = child.stdin.take().expect("piped stdin");
            if let Err(e) = stdin.write_all(script.as_bytes()) {
                return (
                    CheckStatus::Unknown,
                    None,
                    Some(format!("write failed: {e}")),
                    started.elapsed().as_millis(),
                );
            }
        }

        // Hard watchdog: Z3 honors :timeout for most QF_BV queries, but never block the
        // service on an unresponsive process.
        let wait_result = wait_with_timeout(child, self.wall_timeout);
        let elapsed = started.elapsed().as_millis();
        match wait_result {
            WaitOutcome::Done(output) => {
                let stdout = String::from_utf8_lossy(&output.stdout);
                let stderr = String::from_utf8_lossy(&output.stderr);
                let (status, model, reason) = parse_response(&stdout, &stderr);
                (status, model, reason, elapsed)
            }
            WaitOutcome::TimedOut(mut child) => {
                let _ = child.kill();
                let _ = child.wait();
                (
                    CheckStatus::Unknown,
                    None,
                    Some(format!("wall-clock timeout after {:?}", self.wall_timeout)),
                    elapsed,
                )
            }
            WaitOutcome::WaitError(e) => (
                CheckStatus::Unknown,
                None,
                Some(format!("wait failed: {e}")),
                elapsed,
            ),
        }
    }
}

impl SmtSolver for Z3Cli {
    fn check(
        &self,
        width: Width,
        inputs: &[String],
        assumptions: &[Term],
    ) -> Result<CheckResult, SolverError> {
        let script = smt::build_query(width, inputs, assumptions, self.timeout_ms)?;
        let (status, model, reason, elapsed_ms) = self.run_query(&script);
        Ok(CheckResult {
            status,
            model,
            reason,
            solver: "z3-cli".to_string(),
            solver_version: self.version.clone(),
            elapsed_ms,
        })
    }

    fn name(&self) -> &str {
        "z3-cli"
    }

    fn version(&self) -> &str {
        &self.version
    }
}

struct Captured {
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

enum WaitOutcome {
    Done(Captured),
    TimedOut(std::process::Child),
    WaitError(std::io::Error),
}

fn wait_with_timeout(mut child: std::process::Child, timeout: Duration) -> WaitOutcome {
    // Poll on a short cadence; bounded programs make this negligible, and it keeps
    // the implementation free of additional dependencies.
    let step = Duration::from_millis(20);
    let mut waited = Duration::ZERO;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => {
                let mut stdout = Vec::new();
                let mut stderr = Vec::new();
                use std::io::Read;
                if let Some(mut o) = child.stdout.take() {
                    let _ = o.read_to_end(&mut stdout);
                }
                if let Some(mut e) = child.stderr.take() {
                    let _ = e.read_to_end(&mut stderr);
                }
                return WaitOutcome::Done(Captured { stdout, stderr });
            }
            Ok(None) => {
                if waited >= timeout {
                    return WaitOutcome::TimedOut(child);
                }
                std::thread::sleep(step);
                waited += step;
            }
            Err(e) => return WaitOutcome::WaitError(e),
        }
    }
}

fn probe_version(bin: &str) -> Result<String, std::io::Error> {
    let out = Command::new(bin).arg("--version").output()?;
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Parse the first `sat|unsat|unknown` line and every `(get-value (...))` result.
fn parse_response(stdout: &str, stderr: &str) -> (CheckStatus, Option<Model>, Option<String>) {
    let mut status = CheckStatus::Unknown;
    let mut saw_token = false;
    for line in stdout.lines() {
        let t = line.trim();
        if t == "sat" {
            status = CheckStatus::Sat;
            saw_token = true;
            break;
        } else if t == "unsat" {
            status = CheckStatus::Unsat;
            saw_token = true;
            break;
        } else if t == "unknown" {
            status = CheckStatus::Unknown;
            saw_token = true;
            break;
        }
    }

    if !saw_token {
        let note = if stderr.trim().is_empty() {
            "solver produced no check-sat verdict".to_string()
        } else {
            format!(
                "solver produced no check-sat verdict; stderr: {}",
                stderr.trim()
            )
        };
        return (CheckStatus::Unknown, None, Some(note));
    }

    if status != CheckStatus::Sat {
        return (status, None, None);
    }

    match parse_model_values(stdout) {
        Ok(model) => (CheckStatus::Sat, Some(model), None),
        Err(e) => (
            // SAT without a parseable model must not be treated as evidence.
            CheckStatus::Unknown,
            None,
            Some(format!("sat but model unreadable: {e}")),
        ),
    }
}

/// Extract `((|x| (_ bv10 8)))` (and `#x0a` / `#b...` forms) from get-value output.
fn parse_model_values(stdout: &str) -> Result<Model, String> {
    let mut model = Model::new();
    let mut rest = stdout;
    while let Some(start) = rest.find("((") {
        let after = &rest[start + 1..];
        let end = after
            .find("))")
            .ok_or_else(|| "unbalanced get-value tuple".to_string())?;
        let pair = &after[..=end]; // "(name value)"
        parse_one_value(pair, &mut model)?;
        rest = &after[end + 2..];
    }
    Ok(model)
}

fn parse_one_value(pair: &str, model: &mut Model) -> Result<(), String> {
    let pair = pair.trim();
    let inner = pair
        .strip_prefix('(')
        .and_then(|s| s.strip_suffix(')'))
        .ok_or_else(|| format!("malformed value pair: {pair}"))?;
    let inner = inner.trim();
    // First token is the symbol: |name|
    if !inner.starts_with('|') {
        return Err(format!("expected quoted symbol in {pair}"));
    }
    let close = inner[1..]
        .find('|')
        .map(|p| p + 1)
        .ok_or_else(|| format!("unterminated symbol in {pair}"))?;
    let name = inner[1..close].to_string();
    let value_txt = inner[close + 1..].trim().to_string();
    let value = parse_bv_literal(&value_txt)?;
    model.insert(name, value);
    Ok(())
}

/// Parse the bitvector literal forms Z3 can print:
/// * `(_ bv10 8)`
/// * `#x0a` (hex), `#b00001010` (binary)
pub fn parse_bv_literal(txt: &str) -> Result<u64, String> {
    let t = txt.trim();
    if let Some(h) = t.strip_prefix("#x") {
        return u64::from_str_radix(h, 16).map_err(|e| e.to_string());
    }
    if let Some(b) = t.strip_prefix("#b") {
        if b.is_empty() {
            return Err("empty binary literal".into());
        }
        return u64::from_str_radix(b, 2).map_err(|e| e.to_string());
    }
    if t.starts_with("(_") {
        let bv = t
            .split_whitespace()
            .find(|tok| tok.starts_with("bv") && tok[2..].chars().all(|c| c.is_ascii_digit()))
            .ok_or_else(|| format!("no bv token in {t}"))?;
        return bv[2..].parse::<u64>().map_err(|e| e.to_string());
    }
    if let Ok(n) = t.parse::<u64>() {
        return Ok(n);
    }
    Err(format!("unrecognized bitvector literal: {t}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_bv_literals() {
        assert_eq!(parse_bv_literal("(_ bv10 8)").unwrap(), 10);
        assert_eq!(parse_bv_literal("#x0a").unwrap(), 10);
        assert_eq!(parse_bv_literal("#b1010").unwrap(), 10);
        assert_eq!(parse_bv_literal("255").unwrap(), 255);
        assert!(parse_bv_literal("#xzz").is_err());
    }

    #[test]
    fn parses_sat_and_values() {
        let out = "sat\n((|x| (_ bv10 8)))\n((|y| (_ bv255 8)))\n";
        let (st, m, r) = parse_response(out, "");
        assert_eq!(st, CheckStatus::Sat);
        assert!(r.is_none());
        let m = m.unwrap();
        assert_eq!(m["x"], 10);
        assert_eq!(m["y"], 255);
    }

    #[test]
    fn parses_unsat() {
        let (st, m, _) = parse_response("unsat\n", "");
        assert_eq!(st, CheckStatus::Unsat);
        assert!(m.is_none());
    }

    #[test]
    fn unknown_is_not_success() {
        let (st, _, r) = parse_response("unknown\n(reason-unknown)\n", "");
        assert_eq!(st, CheckStatus::Unknown);
        assert!(r.is_none());
        let (st, _, r) = parse_response("garbage", "boom");
        assert_eq!(st, CheckStatus::Unknown);
        assert!(r.unwrap().contains("boom"));
    }
}
